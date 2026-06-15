from __future__ import annotations
#!/usr/bin/env python3
# =============================================================================
# main.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: Entry point for the Jetson Orin Nano smart chessboard.
#          Coordinates hardware init, game-mode selection, the main game loop,
#          Stockfish / Lichess integration, web dashboard, and OLED display.
#
# New in this revision
# ────────────────────
#  • Local two-player mode (pass-and-play, mode 3)
#  • Pawn promotion selector (Q R B N via buttons 1-4)
#  • Undo last move (button combo: btn8 + hint)
#  • Per-player chess clock shown on OLED
#  • Eval bar on OLED (small bar under whose-turn strip)
#  • Voice announcements (silent if no USB speaker connected)
#  • USB game save after each game (only if drive plugged in)
#  • Post-game blunder analysis (top-3 blunders, OLED + web)
#  • Web dashboard: move input, undo, custom theme, USB save, OTA
#  • Fast startup LED sweep (15 ms/square instead of 1 s)
#  • Eval calculated via StockfishEngine.evaluate() — no process leak
#  • GameConfig dataclass centralises all magic numbers
# =============================================================================

import time
import logging
import logging.handlers
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

log = logging.getLogger("main")


# ── Constants ─────────────────────────────────────────────────────────────────

PROMOTION_MAP = {1: "q", 2: "r", 3: "b", 4: "n"}
PROMOTION_NAME = {"q": "queen", "r": "rook", "b": "bishop", "n": "knight"}


class ChessGame:
    def __init__(self):
        log.info("Initialising Jetson Smart Chess Board v3...")

        load_dotenv()

        # Buttons MUST init before LEDs — rpi_ws281x claims GPIO.BCM
        # internally when the strip starts; ButtonController needs to set
        # BOARD mode first or Jetson.GPIO raises "mode already set".
        self.buttons   = ButtonController()
        self.leds      = LEDController()
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

        # Web setup — used during mode/difficulty/time selection from browser
        self._web_setup_answer = None
        self._web_setup_event  = threading.Event()
        self._setup_preview_val: int = 0   # live value from slider drag

        # Web mode select
        self._web_mode_queue: list = []
        self._web_mode_event  = threading.Event()

        # Which side the web controls in LocalHuman mode (None = buttons only)
        self._web_player: str | None = None

        # Hint press counter for disco easter egg
        self._hint_count = 0
        self._last_hint_time_disco = 0.0

        web_server.register_callbacks(
            new_game         = self._new_game_sequence,
            hint             = lambda: self._hint_isr(None),
            set_theme        = self.anim.set_theme,
            move_input       = self._web_move_received,
            undo             = self._undo_sequence,
            save_usb         = self._save_to_usb,
            toggle_voice     = self._toggle_voice,
            web_mode_select  = self._web_mode_received,
            web_setup_answer = self._web_setup_received,
            slider_preview   = self._slider_preview,
            disco            = self._disco_mode,
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

        self.oled.show_network_info(self._get_local_ip())
        time.sleep(2.0)
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
            # Eval is cached from last push_move — don't block here
            self.oled.show_game(current, status="Your move...")
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

            if is_checkmate:
                pass  # handled below in _stockfish_turn or LocalHuman end
            elif cb.is_stalemate():
                self._end_game_draw("Stalemate")
                return
            elif cb.is_insufficient_material():
                self._end_game_draw("Insufficient material")
                return
            elif cb.is_seventyfive_moves():
                self._end_game_draw("75-move rule")
                return
            elif is_check:
                king_sq = cb.king(cb.turn)
                if king_sq is not None:
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
                self.oled.show_pass_board(next_turn, self.board.move_count())
                if self._voice_enabled:
                    voice.announce_status(f"Pass the board to {next_turn}")
                # Give time to hand over the board
                time.sleep(3.0)

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
            if king_sq is not None:
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
        web_server.update_state(setup_phase="mode_select")

        if self._voice_enabled:
            voice.announce_status(
                "Select game mode. Press 1 for AI, 2 for Lichess, 3 for local two player."
            )

        # Run button scan in a background thread so web events aren't blocked
        _mode_result: list = []
        _mode_done = threading.Event()

        def _btn_scan():
            while not _mode_done.is_set():
                btn = self.buttons.detect_button_nowait()
                if btn in (1, 2, 3):
                    _mode_result.append(btn)
                    _mode_done.set()
                    return
                time.sleep(0.01)

        _scan_thread = threading.Thread(target=_btn_scan, daemon=True)
        _scan_thread.start()

        while not _mode_done.is_set():
            # Check web mode selection
            if self._web_mode_event.wait(timeout=0.05):
                self._web_mode_event.clear()
                if self._web_mode_queue:
                    _mode_done.set()
                    self._apply_web_mode(self._web_mode_queue.pop(0))
                    break
                continue

        if _mode_result:
            btn = _mode_result[0]
            if btn == 1:
                self.game_mode = "Stockfish"
                self._web_player = None
                self.display.confirm_ai_mode()
                if self._voice_enabled:
                    voice.announce_status("AI mode selected")
            elif btn == 2:
                self.game_mode = "OnlineHuman"
                self._web_player = None
                self.display.confirm_online_mode()
                if self._voice_enabled:
                    voice.announce_status("Online mode selected")
            elif btn == 3:
                self.game_mode = "LocalHuman"
                self._web_player = None
                self.display.confirm_local_mode()
                if self._voice_enabled:
                    voice.announce_status("Local two player mode selected")

        web_server.update_state(game_mode=self.game_mode, setup_phase="idle",
                                web_player=self._web_player)
        log.info(f"Mode: {self.game_mode}, web_player: {self._web_player}")

    def _apply_web_mode(self, web_mode: str):
        """Map web mode string to internal game_mode + web_player."""
        if web_mode == "stockfish":
            self.game_mode = "Stockfish"; self._web_player = None
        elif web_mode == "lichess":
            self.game_mode = "OnlineHuman"; self._web_player = None
        elif web_mode == "local":
            self.game_mode = "LocalHuman"; self._web_player = None
        elif web_mode == "web_vs_ai":
            self.game_mode = "Stockfish"; self._web_player = "White"
        elif web_mode == "web_local":
            self.game_mode = "LocalHuman"; self._web_player = "both"
        else:
            self.game_mode = "Stockfish"; self._web_player = None

    def _setup_game(self):
        self.leds.control_panel_fill((255, 255, 255), start=0, count=4)
        self.leds.panel_show()

        if self.game_mode == "Stockfish":
            # Difficulty
            self.oled.show_setup_difficulty()
            self.display.show_difficulty_icon()
            web_server.update_state(setup_phase="difficulty", difficulty=CFG.difficulty_default)
            if self._voice_enabled:
                voice.announce_status("Set AI difficulty")
            self._setup_preview_val = 1  # start at 1
            self.oled.show_setup_difficulty(1)  # show bar at 1 immediately
            ans = self._wait_setup_answer(phase="difficulty")
            ans_int = int(ans) if ans else CFG.difficulty_default
            if ans_int <= 8:
                self.difficulty = self._map_range(ans_int, 1, 8,
                                                  CFG.difficulty_min,
                                                  CFG.difficulty_max)
            else:
                self.difficulty = max(CFG.difficulty_min,
                                      min(CFG.difficulty_max, ans_int))
            self._setup_preview_val = 0
            self.oled.show_setup_difficulty(self.difficulty)
            web_server.update_state(difficulty=self.difficulty, setup_phase="time")

            # Move time
            self.oled.show_setup_timeout()
            self.display.show_timeout_icon()
            if self._voice_enabled:
                voice.announce_status("Set move time")
            self._setup_preview_val = 5
            ans = self._wait_setup_answer(phase="time")
            # Both web and buttons send 1-8 index
            TIME_MS = [1000, 2000, 3000, 5000, 8000, 12000, 20000, 30000]
            idx = max(1, min(8, int(ans) if ans else 5))
            self.move_timeout_ms = TIME_MS[idx - 1]
            self.oled.show_setup_timeout(self.move_timeout_ms)
            web_server.update_state(setup_phase="idle")

            # Web player colour if web_vs_ai mode
            if self._web_player:
                web_server.update_state(setup_phase="web_colour")
                ans = self._wait_setup_answer()
                self._web_player = str(ans) if ans else "White"
                web_server.update_state(setup_phase="idle", web_player=self._web_player)

        elif self.game_mode == "OnlineHuman":
            self.oled.show_setup_colour()
            self.display.show_colour_choice_icon()
            web_server.update_state(setup_phase="colour")
            if self._voice_enabled:
                voice.announce_status("Choose your colour")
            ans = self._wait_setup_answer()
            if ans in ("white", "black"):
                self.colour_choice = ans
            else:
                self.colour_choice = "white"
            web_server.update_state(setup_phase="idle")
            self.lichess.start_game(self.colour_choice)

        elif self.game_mode == "LocalHuman":
            self.oled.show_local_game_start()
            if self._web_player == "both":
                web_server.update_state(setup_phase="idle")
            if self._voice_enabled:
                voice.announce_status("Local two player. White plays first. Good luck!")
            time.sleep(1.5)

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

    def _end_game_draw(self, reason: str):
        """Handle drawn game (stalemate, insufficient material, etc.)."""
        self.oled.show_draw(reason)
        web_server.update_state(
            status=f"Draw — {reason}",
            game_active=False,
            draw_reason=reason,
        )
        if self._voice_enabled:
            voice.announce_status(f"Draw! {reason}.")
        self.anim.rainbow_victory(duration=2.0)
        self._save_to_usb()

    def _end_game(self, winner: str):
        self.oled.show_checkmate(winner)
        web_server.update_state(status=f"Checkmate! {winner} wins!", game_active=False)
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
        """Get a move from the current human player (buttons or web)."""
        current = self._current_turn()

        # Web-only player: just wait for a web move
        web_controls = (
            self._web_player in ("both", current)
        )

        if web_controls:
            self._web_move_event.clear()
            web_server.update_state(status=f"Your move ({current}) — click a piece")
            while True:
                if self._web_move_event.wait(timeout=0.1):
                    self._web_move_event.clear()
                    if self._web_move_queue:
                        return self._web_move_queue.pop(0)
                if self._new_game_requested.is_set():
                    raise _NewGameException()

        # Button player (with optional web override)
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
                if self._web_move_event.wait(timeout=0.05):
                    self._web_move_event.clear()
                    if self._web_move_queue:
                        web_uci = self._web_move_queue.pop(0)
                        self.leds.control_panel_fill((0, 0, 0), start=0, count=6)
                        self.leds.panel_show()
                        return web_uci
                btn = self.buttons.detect_button_nowait() or 0
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
        col_names = {1:"A", 2:"B", 3:"C", 4:"D", 5:"E", 6:"F", 7:"G", 8:"H"}
        row_names = {1:"1", 2:"2", 3:"3", 4:"4", 5:"5", 6:"6", 7:"7", 8:"8"}

        column = None
        while column is None:
            btn = already_pressed if already_pressed != 0 else self.buttons.detect_button()
            already_pressed = 0
            column = col_map.get(btn)
            if column:
                self.oled.show_status(f"Column {col_names[btn]} — now press row (1-8)")
        time.sleep(0.1)
        row = None
        while row is None:
            btn = self.buttons.detect_button()
            row = row_map.get(btn)
            if row:
                self.oled.show_status(f"→ {column.upper()}{row}")
        time.sleep(0.1)
        return column + row

    def _web_move_received(self, uci: str):
        """Called from web server thread when a browser move arrives."""
        self._web_move_queue.append(uci)
        self._web_move_event.set()
        log.info(f"Web move queued: {uci}")

    def _web_mode_received(self, mode: str):
        """Called from web server when browser selects a game mode."""
        self._web_mode_queue.append(mode)
        self._web_mode_event.set()
        log.info(f"Web mode queued: {mode}")

    def _slider_preview(self, phase: str, value: int):
        """Called live on every slider drag — updates OLED and stores preview value."""
        self._setup_preview_val = value
        if phase == "difficulty":
            self.oled.show_setup_difficulty(value)
        elif phase == "time":
            # value is 1-8 index; map to seconds for OLED display
            TIME_OPTIONS = [1, 2, 3, 5, 8, 12, 20, 30]
            secs = TIME_OPTIONS[min(value - 1, 7)]
            self.oled.show_setup_timeout(secs * 1000)

    def _web_setup_received(self, value):
        """Called from web server when browser answers a setup prompt.
        value can be:
          - a plain int/str → final answer (confirm button pressed)
          - a dict {preview: True, value: N} → live slider drag
        """
        self._web_setup_answer = value
        self._web_setup_event.set()
        log.info(f"Web setup answer: {value}")


    def _wait_setup_answer(self, phase: str = ""):
        """Block until setup answer arrives from web confirm or physical buttons.
        Live slider preview is handled separately via _slider_preview callback.
        OK button (btn 9) confirms whatever _setup_preview_val is currently set to.
        Any other button 1-8 confirms immediately with that button number.
        """
        self._web_setup_event.clear()
        self._web_setup_answer = None

        while True:
            if self._web_setup_event.wait(timeout=0.03):
                self._web_setup_event.clear()
                return self._web_setup_answer

            btn = self.buttons.detect_button_nowait()
            if btn == 9:
                # OK confirms current preview value, or falls back to default
                return self._setup_preview_val or 4
            elif btn and btn != 0:
                return btn
            time.sleep(0.02)

    def _disco_mode(self):
        """Easter egg: disco light show on the chess board LEDs."""
        import random
        log.info("DISCO MODE ACTIVATED")
        self.anim.stop_thinking_animation()
        web_server.update_state(disco_active=True, status="DISCO MODE!")
        # OLED: show text without emoji (OLED font can't render them)
        self.oled.show_status("DISCO MODE!")
        end = time.time() + 15.0
        while time.time() < end:
            for i in range(64):
                r = random.randint(0, 255)
                g = random.randint(0, 255)
                b = random.randint(0, 255)
                col = i % 8
                row = i // 8
                self.leds.chess_set_pixel(col, row, (r, g, b))
            self.leds.chess_show()
            time.sleep(0.05)
        # Restore board state after disco ends
        web_server.update_state(disco_active=False, status="Your move")
        self.anim.show_board_themed()
        # Restore OLED to current game screen
        self.oled.show_game(
            self._current_turn(),
            status="Your move...",
            move_history=self.board._move_history_uci(),
        )

    # ── Validation ─────────────────────────────────────────────────────────

    def _check_move_legal(self, move_uci: str) -> bool:
        for _ in range(10):
            result = self.stockfish.is_legal(self.board.fen(), move_uci)
            if result is not None:
                return result
            time.sleep(0.05)
        log.warning(f"is_legal timed out for {move_uci} — rejecting")
        return False

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

        # Count rapid hint presses for disco easter egg (10 in 5 seconds)
        now = time.time()
        if now - self._last_hint_time_disco < 5.0:
            self._hint_count += 1
        else:
            self._hint_count = 1
        self._last_hint_time_disco = now
        if self._hint_count >= 10:
            self._hint_count = 0
            threading.Thread(target=self._disco_mode, daemon=True).start()
            return

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
        self._web_player = None
        self._hint_count = 0
        self.leds.control_panel_set_pixel(5, (0, 0, 0))
        self.leds.control_panel_fill((255, 255, 255), start=0, count=5)
        self.leds.panel_show()
        self.oled.show_new_game()
        self.anim.board_wipe((0, 0, 0), direction="left")
        self.display.loading_animation_fast()
        time.sleep(1)
        web_server.update_state(
            fen=self.board.fen(),
            move_history=[],
            whose_turn="White",
            status="New game — select mode",
            last_move=None,
            hint_move="",
            game_active=False,
            usb_available=usb.is_available(),
        )
        # Always go through full mode select + setup for new game
        self._choose_game_mode()
        self._setup_game()
        self.anim.show_board_themed()
        self.oled.show_game("White", status="New game! Your move.")
        web_server.update_state(game_active=True, status="Game started",
                                whose_turn="White")
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
    def _get_local_ip() -> str:
        """Return the local network IP address for the web dashboard URL."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "localhost"

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