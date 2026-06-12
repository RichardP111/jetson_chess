#!/usr/bin/env python3
# =============================================================================
# main.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: Entry point for the Jetson Orin Nano smart chessboard.
#          Coordinates hardware init, game-mode selection, the main game loop,
#          Stockfish / Lichess integration, web dashboard, and OLED display.
#
# =============================================================================

import time
import logging
import sys
import signal
import threading

import chess as _chess

from dotenv import load_dotenv

from config import CFG
from hardware.leds import LEDController
from hardware.buttons import ButtonController
from chess_engine.board_state import BoardState
from chess_engine.stockfish import StockfishEngine
from online.lichess import LichessClient
from ui.display import Display
from ui.animations import AnimationEngine
from oled.oled_display import OLEDDisplay
from voice import voice
from usb_storage import usb
import web.server as web_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            "/tmp/chess.log", maxBytes=1_000_000, backupCount=3
        ),
    ],
)
import logging.handlers  # noqa: E402 — import after basicConfig so handler is available

log = logging.getLogger("main")


# ── Constants ─────────────────────────────────────────────────────────────────

PROMOTION_MAP = {1: "q", 2: "r", 3: "b", 4: "n"}
PROMOTION_NAME = {"q": "queen", "r": "rook", "b": "bishop", "n": "knight"}


class ChessGame:
    def __init__(self):
        log.info("Initialising Jetson Smart Chess Board v4...")

        load_dotenv()

        self.leds      = LEDController()
        self.buttons   = ButtonController()
        self.board     = BoardState()
        self.display   = Display(self.leds, self.board, self.buttons)
        self.anim      = AnimationEngine(self.leds, self.board)
        self.oled      = OLEDDisplay()
        self.stockfish = StockfishEngine()
        self.lichess   = LichessClient()

        self.game_mode           = None
        self.colour_choice       = None
        self.difficulty          = CFG.difficulty_default
        self.move_timeout_ms     = CFG.movetime_default_ms
        self.suggested_best_move = ""
        self._voice_enabled      = voice.available

        # Per-player clocks (elapsed seconds)
        self._clock = {"White": 0.0, "Black": 0.0}
        self._clock_start: float | None = None
        self._active_colour: str = "White"

        self._new_game_requested = threading.Event()
        self._web_move_queue: list = []
        self._web_move_event  = threading.Event()

        web_server.register_callbacks(
            new_game     = self._new_game_sequence,
            hint         = lambda: self._hint_isr(None),
            set_theme    = self.anim.set_theme,
            move_input   = self._web_move_received,
            undo         = self._undo_sequence,
            save_usb     = self._save_to_usb,
            toggle_voice = self._toggle_voice,
        )
        web_server.start_server()
        web_server.update_state(voice_enabled=self._voice_enabled)

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        self.buttons.register_hint_callback(self._hint_isr)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def run(self):
        self.oled.show_idle()
        self.display.show_opening_markings()
        self.anim.rain_effect(duration=2.0)

        self._startup_sequence()
        while True:
            self._new_game_requested.clear()
            try:
                self._game_loop()
            except _NewGameException:
                log.info("New game requested")
                self._new_game_sequence()
            except KeyboardInterrupt:
                self._shutdown()

    def _startup_sequence(self):
        # Fast sweep: ~15 ms per pixel = ~1 second total (was 64 seconds)
        for i in range(64):
            row = i // 8
            col = i % 8
            self.leds.chess_set_pixel(col, row, (0, 255, 0))
            self.leds.chess_show()
            self.oled.show_loading(i + 1, 64)
            time.sleep(CFG.startup_led_delay_s)

        self._choose_game_mode()
        self._setup_game()
        self.anim.show_board_themed()
        self.oled.show_game(self._current_turn(), status="Game ready!")
        web_server.update_state(game_active=True, status="Game started",
                                usb_available=usb.is_available())

    def _game_loop(self):
        while True:
            if self._new_game_requested.is_set():
                raise _NewGameException()

            current = self._current_turn()
            self.oled.show_game(current, status="Your move...",
                                eval_score=self.stockfish.evaluate(self.board.fen()))
            web_server.update_state(whose_turn=current, status="Your move",
                                    usb_available=usb.is_available())

            if voice.available and self._voice_enabled:
                voice.announce_status(f"{current}'s turn")

            # ── Get the human's move ───────────────────────────────────────
            self._start_clock(current)
            humans_move = self._humans_go()
            self._stop_clock(current)
            log.info(f"Human move entered: {humans_move}")

            # ── Promotion check ────────────────────────────────────────────
            if self.board.is_promotion(humans_move):
                humans_move = self._ask_promotion(humans_move)

            self.anim.move_trail(humans_move[:2], humans_move[2:4])
            self.display.light_up_move(humans_move, mode="Y")

            legal = self._check_move_legal(humans_move)
            if not legal:
                log.warning("Illegal move")
                self.display.error_animation()
                self.anim.show_board_themed()
                self.oled.show_status("Illegal move — try again")
                if self._voice_enabled:
                    voice.announce_status("Illegal move, try again")
                continue

            captured   = self.board.is_capture(humans_move)
            self.board.apply_move(humans_move)
            self._push_move_to_ui(humans_move, "Human")

            # ── Check detection ────────────────────────────────────────────
            cb = _chess.Board(self.board.fen())
            is_check     = cb.is_check()
            is_checkmate = cb.is_checkmate()

            if is_check and not is_checkmate:
                king_sq = cb.king(cb.turn)
                self.anim.check_alert(king_sq % 8, 7 - (king_sq // 8))
                self.oled.show_status("CHECK!")
                web_server.update_state(status="CHECK!")
                if self._voice_enabled:
                    voice.announce_status("Check!")

            if self._voice_enabled:
                voice.announce_move(humans_move, captured=captured,
                                    is_check=is_check and not is_checkmate,
                                    is_checkmate=is_checkmate)

            self.anim.show_board_themed()

            # ── Opponent response ──────────────────────────────────────────
            if self.game_mode == "Stockfish":
                self._stockfish_turn(is_checkmate)

            elif self.game_mode == "OnlineHuman":
                self._online_turn(humans_move)

            elif self.game_mode == "LocalHuman":
                # Just swap turns; the other human uses the same input loop
                next_turn = self._current_turn()
                self.oled.show_game(next_turn, status=f"Pass to {next_turn}",
                                    move_history=self.board._move_history_uci())
                if self._voice_enabled:
                    voice.announce_status(f"Pass to {next_turn}")
                # Brief pause so the player can hand over the board
                time.sleep(1.5)

            self.anim.show_board_themed()
            self.oled.show_game(
                self._current_turn(),
                last_move=self.board._move_history_uci()[-1]
                          if self.board._move_history_uci() else "",
                move_history=self.board._move_history_uci(),
                eval_score=self.stockfish.evaluate(self.board.fen()),
            )

    # ── Stockfish response ─────────────────────────────────────────────────

    def _stockfish_turn(self, human_caused_checkmate: bool):
        if human_caused_checkmate:
            self._end_game("Human")
            return

        self.oled.show_status("Engine thinking...")
        web_server.update_state(thinking=True, status="Engine thinking...")
        self.anim.start_thinking_animation()

        engine_move, best_hint = self.stockfish.get_move(
            self.board.fen(),
            skill_level=self.difficulty,
            movetime_ms=self.move_timeout_ms,
        )
        self.anim.stop_thinking_animation()
        web_server.update_state(thinking=False)
        self.suggested_best_move = best_hint

        # Eval via the already-open engine (no process leak)
        eval_score = self.stockfish.evaluate(self.board.fen())
        web_server.update_state(eval_score=eval_score)

        captured_by_engine = self.board.is_capture(engine_move)
        self.anim.move_trail(engine_move[:2], engine_move[2:4])
        self.display.light_up_move(engine_move, mode="N")
        log.info(f"Engine move: {engine_move}")

        cb_before = _chess.Board(self.board.fen())
        self.board.apply_move(engine_move)
        self._push_move_to_ui(engine_move, "Computer")

        cb_after    = _chess.Board(self.board.fen())
        is_check    = cb_after.is_check()
        is_checkmate= cb_after.is_checkmate()

        if self._voice_enabled:
            voice.announce_move(engine_move, captured=captured_by_engine,
                                is_check=is_check and not is_checkmate,
                                is_checkmate=is_checkmate)

        if is_check and not is_checkmate:
            king_sq = cb_after.king(cb_after.turn)
            self.anim.check_alert(king_sq % 8, 7 - (king_sq // 8))
            self.oled.show_status("CHECK!")
            web_server.update_state(status="CHECK!")

        self._check_for_checkmate(best_hint, engine_move)

    # ── Online (Lichess) response ──────────────────────────────────────────

    def _online_turn(self, humans_move: str):
        self.lichess.send_move(humans_move)
        self.oled.show_status("Waiting for opponent...")
        web_server.update_state(status="Waiting for opponent...")
        opponent_move = self.lichess.wait_for_move()
        log.info(f"Online opponent move: {opponent_move}")
        captured = self.board.is_capture(opponent_move)
        self.anim.move_trail(opponent_move[:2], opponent_move[2:4])
        self.display.light_up_move(opponent_move, mode="N")
        self.board.apply_move(opponent_move)
        self._push_move_to_ui(opponent_move, "Opponent")
        if self._voice_enabled:
            voice.announce_move(opponent_move, captured=captured)

    # ── Setup ──────────────────────────────────────────────────────────────

    def _choose_game_mode(self):
        log.info("Waiting for game mode selection...")
        self.oled.show_mode_select()
        self.leds.control_panel_fill((255, 255, 255), start=0, count=5)
        self.leds.panel_show()

        if self._voice_enabled:
            voice.announce_status(
                "Select game mode. Press 1 for Stockfish, 2 for Lichess, 3 for local two player."
            )

        while True:
            btn = self.buttons.detect_button()
            time.sleep(0.3)
            if btn == 1:
                self.game_mode = "Stockfish"
                self.display.confirm_ai_mode()
                if self._voice_enabled:
                    voice.announce_status("AI mode selected")
                break
            elif btn == 2:
                self.game_mode = "OnlineHuman"
                self.display.confirm_online_mode()
                if self._voice_enabled:
                    voice.announce_status("Online mode selected")
                break
            elif btn == 3:
                self.game_mode = "LocalHuman"
                self.display.confirm_local_mode()
                if self._voice_enabled:
                    voice.announce_status("Local two player mode selected")
                break

        web_server.update_state(game_mode=self.game_mode)
        log.info(f"Mode: {self.game_mode}")

    def _setup_game(self):
        self.leds.control_panel_fill((255, 255, 255), start=0, count=4)
        self.leds.panel_show()

        if self.game_mode == "Stockfish":
            self.oled.show_setup_difficulty()
            self.display.show_difficulty_icon()
            if self._voice_enabled:
                voice.announce_status("Press buttons 1 to 8 to set difficulty")
            btn = self.buttons.detect_button()
            self.difficulty = self._map_range(btn, 1, 8,
                                              CFG.difficulty_min,
                                              CFG.difficulty_max)
            self.oled.show_setup_difficulty(self.difficulty)
            web_server.update_state(difficulty=self.difficulty)
            time.sleep(0.3)

            self.oled.show_setup_timeout()
            self.display.show_timeout_icon()
            if self._voice_enabled:
                voice.announce_status("Press buttons 1 to 8 to set move time")
            btn = self.buttons.detect_button()
            self.move_timeout_ms = self._map_range(btn, 1, 8,
                                                   CFG.movetime_min_ms,
                                                   CFG.movetime_max_ms)
            self.oled.show_setup_timeout(self.move_timeout_ms)
            time.sleep(0.3)

        elif self.game_mode == "OnlineHuman":
            self.oled.show_setup_colour()
            self.display.show_colour_choice_icon()
            if self._voice_enabled:
                voice.announce_status("Press 1 for White, 2 for Black")
            while True:
                btn = self.buttons.detect_button()
                time.sleep(0.3)
                if btn == 1:
                    self.colour_choice = "white"
                    break
                elif btn == 2:
                    self.colour_choice = "black"
                    break
            self.lichess.start_game(self.colour_choice)

        elif self.game_mode == "LocalHuman":
            self.oled.show_local_game_start()
            if self._voice_enabled:
                voice.announce_status("Local two player. White plays first. Good luck!")
            time.sleep(2)

        self.leds.control_panel_fill((10, 10, 10), start=6, count=16)
        self.leds.panel_show()

    # ── Promotion ──────────────────────────────────────────────────────────

    def _ask_promotion(self, base_uci: str) -> str:
        """
        Show the 4-quadrant board and OLED prompt, wait for button 1-4.
        Returns the full UCI string with promotion piece appended.
        """
        colour = self._current_turn()
        self.display.show_promotion_choices()
        self.oled.show_promotion_select(colour)
        if self._voice_enabled:
            voice.announce_status(
                "Pawn promotion! Press 1 queen, 2 rook, 3 bishop, 4 knight"
            )

        piece = "q"  # default safety
        while True:
            btn = self.buttons.detect_button()
            if btn in PROMOTION_MAP:
                piece = PROMOTION_MAP[btn]
                break

        self.anim.promotion_flash(piece)
        full_uci = self.board.promotion_move(base_uci, piece)
        log.info(f"Promotion selected: {piece} → {full_uci}")
        if self._voice_enabled:
            voice.announce_status(f"Promoting to {PROMOTION_NAME[piece]}")
        return full_uci

    # ── Undo ───────────────────────────────────────────────────────────────

    def _undo_sequence(self):
        """Undo the last two half-moves (human + engine/opponent)."""
        if self.board.move_count() < 1:
            self.oled.show_status("Nothing to undo")
            return

        # In Stockfish mode undo 2 half-moves; in other modes undo 1
        count = 2 if self.game_mode == "Stockfish" and self.board.move_count() >= 2 else 1
        undone = self.board.undo_half_moves(count)

        self.anim.undo_sweep()
        self.display.show_undo_icon()
        self.oled.show_undo_confirm(undone)
        web_server.update_state(
            fen=self.board.fen(),
            move_history=self.board._move_history_uci(),
            whose_turn=self._current_turn(),
            status=f"Undid {undone} half-move(s)",
            last_move=self.board._move_history_uci()[-1]
                      if self.board._move_history_uci() else None,
        )
        if self._voice_enabled:
            voice.announce_status(f"Undone. {self._current_turn()}'s turn.")
        time.sleep(1.5)
        self.anim.show_board_themed()
        self.suggested_best_move = ""

    # ── Game end ───────────────────────────────────────────────────────────

    def _end_game(self, winner: str):
        self.oled.show_checkmate(winner)
        web_server.update_state(status=f"Checkmate! {winner} wins!")
        if self._voice_enabled:
            voice.announce_status(f"Checkmate! {winner} wins!")
        self.anim.rainbow_victory(duration=4.0)

        # Post-game analysis (background thread so LEDs keep running)
        threading.Thread(target=self._run_analysis, daemon=True).start()

        # USB save (if drive present)
        self._save_to_usb()

    def _run_analysis(self):
        """Analyse the game for top-3 blunders and show on OLED."""
        history = self.board._move_history_uci()
        if len(history) < 4:
            return

        blunders = []
        board = _chess.Board()
        prev_score = self.stockfish.evaluate(board.fen())

        for uci in history:
            try:
                move = _chess.Move.from_uci(uci)
                board.push(move)
                score = self.stockfish.evaluate(board.fen(), time_s=0.05)
                # A blunder for the side that just moved is a large swing
                # against them: white move → score should go up; black → down
                loss = prev_score - score  # positive = bad for white mover
                if board.turn == _chess.WHITE:
                    loss = -loss  # flip for black's perspective
                if loss > 100:
                    # Find the best move at the position before this move
                    brd_before = _chess.Board(board.fen())
                    brd_before.pop()
                    _, best = self.stockfish.get_move(brd_before.fen(),
                                                       skill_level=20,
                                                       movetime_ms=200)
                    blunders.append({
                        "move": uci,
                        "loss": int(loss),
                        "best": best or "?",
                    })
                prev_score = score
            except Exception:
                pass

        blunders.sort(key=lambda x: x["loss"], reverse=True)
        top3 = blunders[:3]
        self.oled.show_analysis(top3)
        web_server.update_state(status=f"Analysis: {len(top3)} blunder(s) found")

    # ── USB save ───────────────────────────────────────────────────────────

    def _save_to_usb(self):
        if not usb.is_available():
            log.info("USB save skipped — no drive")
            return
        filename = usb.save_game(
            move_history_uci=self.board._move_history_uci(),
            game_mode=self.game_mode or "Unknown",
            difficulty=self.difficulty,
        )
        if filename:
            self.oled.show_usb_saved(filename)
            web_server.update_state(status=f"Saved: {filename}")
            if self._voice_enabled:
                voice.announce_status("Game saved to USB drive.")
            time.sleep(2)

    # ── Voice toggle ───────────────────────────────────────────────────────

    def _toggle_voice(self):
        if not voice.available:
            self.oled.show_status("No speaker detected")
            web_server.update_state(status="No speaker detected")
            return
        self._voice_enabled = not self._voice_enabled
        state = "ON" if self._voice_enabled else "OFF"
        log.info(f"Voice {state}")
        web_server.update_state(voice_enabled=self._voice_enabled)
        self.oled.show_status(f"Voice {state}")
        if self._voice_enabled:
            voice.announce_status("Voice announcements on")

    # ── Move input ─────────────────────────────────────────────────────────

    def _humans_go(self) -> str:
        btn = 0
        while True:
            self.leds.control_panel_fill((255, 255, 255), start=0, count=4)
            self.leds.panel_show()
            move_from = self._get_coordinates(btn)
            move_to   = self._get_coordinates(0)
            btn = 0
            humans_move = move_from + move_to
            self.leds.control_panel_set_pixel(4, (255, 255, 255))
            self.leds.control_panel_set_pixel(5, (0, 0, 0))
            self.leds.panel_show()
            self.display.light_up_move(humans_move, mode="Y")
            btn = 0
            while btn == 0:
                # Also check for a web dashboard move in the queue
                if self._web_move_event.wait(timeout=0.05):
                    self._web_move_event.clear()
                    if self._web_move_queue:
                        web_uci = self._web_move_queue.pop(0)
                        self.leds.control_panel_fill((0, 0, 0), start=0, count=6)
                        self.leds.panel_show()
                        return web_uci
                btn = self.buttons.detect_button() if not self._web_move_event.is_set() else 0
            if btn == 9:
                self.leds.control_panel_fill((0, 0, 0), start=0, count=6)
                self.leds.panel_show()
                return humans_move
            else:
                self.anim.show_board_themed()

    def _get_coordinates(self, already_pressed: int = 0) -> str:
        col_map = {1: "a", 2: "b", 3: "c", 4: "d",
                   5: "e", 6: "f", 7: "g", 8: "h"}
        row_map = {1: "1", 2: "2", 3: "3", 4: "4",
                   5: "5", 6: "6", 7: "7", 8: "8"}
        column = None
        while column is None:
            btn    = (already_pressed if already_pressed != 0
                      else self.buttons.detect_button())
            already_pressed = 0
            column = col_map.get(btn)
        time.sleep(0.3)
        row = None
        while row is None:
            btn = self.buttons.detect_button()
            row = row_map.get(btn)
        time.sleep(0.3)
        return column + row

    def _web_move_received(self, uci: str):
        """Called from web server thread when a browser move arrives."""
        self._web_move_queue.append(uci)
        self._web_move_event.set()
        log.info(f"Web move queued: {uci}")

    # ── Validation ─────────────────────────────────────────────────────────

    def _check_move_legal(self, move_uci: str) -> bool:
        for _ in range(30):
            result = self.stockfish.is_legal(self.board.fen(), move_uci)
            if result is not None:
                return result
            time.sleep(0.1)
        return True

    def _check_for_checkmate(self, hint: str, attacker_move: str):
        if not hint or len(hint) < 3:
            winner = "Black" if self._current_turn() == "White" else "White"
            self._end_game(winner)

    # ── Hint ISR ───────────────────────────────────────────────────────────

    def _hint_isr(self, channel):
        # Hold btn8 + hint → undo
        if self.buttons and self.buttons.is_button8_held():
            self._undo_sequence()
            return
        # Hold OK + hint → new game
        if self.buttons and self.buttons.is_ok_held():
            self._new_game_requested.set()
            return
        # Hold hint alone → shutdown
        # (single tap is handled below)
        if self.suggested_best_move and len(self.suggested_best_move) >= 3:
            self.leds.control_panel_fill((0, 0, 0), start=0, count=4)
            self.leds.control_panel_set_pixel(5, (0, 0, 255))
            self.leds.panel_show()
            self.oled.show_hint(self.suggested_best_move)
            web_server.update_state(
                hint_move=self.suggested_best_move,
                status=f"Hint: {self.suggested_best_move}",
            )
            # Hint uses move_trail for better visual clarity
            self.anim.move_trail(
                self.suggested_best_move[:2],
                self.suggested_best_move[2:4],
            )
            self.display.light_up_move(self.suggested_best_move, mode="H")
            if self._voice_enabled:
                voice.announce_status(f"Hint: {self.suggested_best_move}")
            self.anim.show_board_themed()
            self.leds.control_panel_set_pixel(5, (255, 255, 255))
            self.leds.control_panel_fill((255, 255, 255), start=0, count=4)
            self.leds.panel_show()
            self.oled.show_game(self._current_turn(), status="Hint shown")
            web_server.update_state(hint_move="", status="Your move")

    # ── New game ───────────────────────────────────────────────────────────

    def _new_game_sequence(self):
        log.info("New game sequence...")
        self.board.reset()
        self.suggested_best_move = ""
        self._clock = {"White": 0.0, "Black": 0.0}
        self._clock_start = None
        self.leds.control_panel_set_pixel(5, (0, 0, 0))
        self.leds.control_panel_fill((255, 255, 255), start=0, count=5)
        self.leds.panel_show()
        self.oled.show_new_game()
        self.anim.board_wipe((0, 0, 0), direction="left")
        self.display.loading_animation_fast()
        time.sleep(1)
        self.anim.show_board_themed()
        self.oled.show_game("White", status="New game! Your move.")
        web_server.update_state(
            fen=self.board.fen(),
            move_history=[],
            whose_turn="White",
            status="New game",
            last_move=None,
            hint_move="",
            usb_available=usb.is_available(),
        )
        self._setup_game()
        if self._voice_enabled:
            voice.announce_status("New game. White to move.")

    # ── Clock ──────────────────────────────────────────────────────────────

    def _start_clock(self, colour: str):
        self._active_colour = colour
        self._clock_start   = time.time()

    def _stop_clock(self, colour: str):
        if self._clock_start is not None:
            self._clock[colour] += time.time() - self._clock_start
            self._clock_start   = None

    # ── Helpers ────────────────────────────────────────────────────────────

    def _current_turn(self) -> str:
        b = _chess.Board(self.board.fen())
        return "White" if b.turn == _chess.WHITE else "Black"

    def _push_move_to_ui(self, uci: str, player: str):
        history    = self.board._move_history_uci()
        whose      = self._current_turn()
        eval_score = self.stockfish.evaluate(self.board.fen())
        self.oled.push_move(uci, whose, eval_score=eval_score)
        web_server.update_state(
            fen=self.board.fen(),
            move_history=history,
            whose_turn=whose,
            last_move=uci,
            status=f"{player}: {uci}",
            eval_score=eval_score,
        )

    @staticmethod
    def _map_range(value, in_min, in_max, out_min, out_max):
        return int((value - in_min) * (out_max - out_min) /
                   (in_max - in_min) + out_min)

    def _shutdown(self, *args):
        log.info("Shutting down...")
        self.anim.stop_thinking_animation()
        self.oled.cleanup()
        self.leds.all_off()
        self.buttons.cleanup()
        self.stockfish.close()
        sys.exit(0)


class _NewGameException(Exception):
    pass


if __name__ == "__main__":
    game = ChessGame()
    game.run()
