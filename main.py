#!/usr/bin/env python3
# =============================================================================
# main.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: Core orchestrator for the Jetson Orin Nano Smart Chess Board.
# =============================================================================

from __future__ import annotations

import os
import sys
import time
import signal
import logging
import threading
import logging.handlers
from typing import Any, Optional

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

PROMOTION_MAP = {1: "q", 2: "r", 3: "b", 4: "n"}
PROMOTION_NAME = {"q": "queen", "r": "rook", "b": "bishop", "n": "knight"}


class ChessGame:
    def __init__(self):
        log.info("Initialising Jetson Smart Chess Board v3...")
        load_dotenv()

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

        self._clock = {"White": 0.0, "Black": 0.0}
        self._clock_start: Optional[float] = None
        self._active_colour: str = "White"

        self._new_game_requested = threading.Event()
        self._web_move_queue: list = []
        self._web_move_event  = threading.Event()

        self._web_setup_answer = None
        self._web_setup_event  = threading.Event()
        self._setup_preview_val: int = 0   

        self._web_mode_queue: list = []
        self._web_mode_event  = threading.Event()

        self._web_player: Optional[str] = None

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
                log.info("New game requested lifecycle event")
                self._new_game_sequence()
            except KeyboardInterrupt:
                self._shutdown()

    def _startup_sequence(self):
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
        web_server.update_state(game_active=True, status="Game started", usb_available=usb.is_available())

    def _game_loop(self):
        while True:
            if self._new_game_requested.is_set():
                raise _NewGameException()

            current = self._current_turn()
            self.oled.show_game(current, status="Your move...")
            web_server.update_state(whose_turn=current, status="Your move", usb_available=usb.is_available())

            if voice.available and self._voice_enabled:
                voice.announce_status(f"{current}'s turn")

            self._start_clock(current)
            humans_move = self._humans_go()
            self._stop_clock(current)
            log.info(f"Human move entered: {humans_move}")

            if self.board.is_promotion(humans_move):
                humans_move = self._ask_promotion(humans_move)

            self.anim.move_trail(humans_move[:2], humans_move[2:4])
            self.display.light_up_move(humans_move, mode="Y")

            legal = self._check_move_legal(humans_move)
            if not legal:
                log.warning("Illegal move processed")
                self.display.error_animation()
                self.anim.show_board_themed()
                self.oled.show_status("Illegal move — try again")
                if self._voice_enabled:
                    voice.announce_status("Illegal move, try again")
                continue

            captured = self.board.is_capture(humans_move)
            self.board.apply_move(humans_move)
            self._push_move_to_ui(humans_move, "Human")

            cb = _chess.Board(self.board.fen())
            is_check     = cb.is_check()
            is_checkmate = cb.is_checkmate()

            if is_checkmate:
                self._end_game("Human")
                return
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

            if self.game_mode == "Stockfish":
                self._stockfish_turn()
            elif self.game_mode == "OnlineHuman":
                self._online_turn(humans_move)
            elif self.game_mode == "LocalHuman":
                next_turn = self._current_turn()
                self.oled.show_pass_board(next_turn, self.board.move_count())
                if self._voice_enabled:
                    voice.announce_status(f"Pass the board to {next_turn}")
                time.sleep(3.0)

            self.anim.show_board_themed()
            self.oled.show_game(
                self._current_turn(),
                last_move=self.board._move_history_uci()[-1] if self.board._move_history_uci() else "",
                move_history=self.board._move_history_uci(),
                eval_score=self.stockfish.evaluate(self.board.fen()),
            )

    def _stockfish_turn(self):
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

        eval_score = self.stockfish.evaluate(self.board.fen())
        web_server.update_state(eval_score=eval_score)

        if not engine_move:
            cb = _chess.Board(self.board.fen())
            if cb.is_checkmate():
                self._end_game("Human")
            else:
                self._end_game_draw("Engine resigned or terminated")
            return

        captured_by_engine = self.board.is_capture(engine_move)
        self.anim.move_trail(engine_move[:2], engine_move[2:4])
        self.display.light_up_move(engine_move, mode="N")
        log.info(f"Engine move: {engine_move}")

        self.board.apply_move(engine_move)
        self._push_move_to_ui(engine_move, "Computer")

        cb_after = _chess.Board(self.board.fen())
        is_check = cb_after.is_check()
        is_checkmate = cb_after.is_checkmate()

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

        if is_checkmate:
            self._end_game("Computer")

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

    def _choose_game_mode(self):
        log.info("Waiting for game mode selection...")
        self.oled.show_mode_select()
        self.leds.control_panel_fill((255, 255, 255), start=0, count=5)
        self.leds.panel_show()
        web_server.update_state(setup_phase="mode_select")

        if self._voice_enabled:
            voice.announce_status("Select game mode. Press 1 for AI, 2 for Lichess, 3 for local two player.")

        _mode_result: list = []
        _mode_done = threading.Event()

        def _btn_scan():
            while not _mode_done.is_set():
                btn = self.buttons.detect_button_nowait()
                if btn in (1, 2, 3):
                    _mode_result.append(btn)
                    _mode_done.set()
                    if not os.environ.get("MOCK_BUTTONS") == "1":
                        while self.buttons._scan_matrix() is not None:
                            time.sleep(0.01)
                        time.sleep(0.3)
                    return
                time.sleep(0.01)

        _scan_thread = threading.Thread(target=_btn_scan, daemon=True)
        _scan_thread.start()

        while not _mode_done.is_set():
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
            elif btn == 2:
                self.game_mode = "OnlineHuman"
                self._web_player = None
                self.display.confirm_online_mode()
            elif btn == 3:
                self.game_mode = "LocalHuman"
                self._web_player = None
                self.display.confirm_local_mode()

        web_server.update_state(game_mode=self.game_mode, setup_phase="idle", web_player=self._web_player)

    def _apply_web_mode(self, web_mode: str):
        if web_mode == "stockfish":
            self.game_mode = "Stockfish"
            self._web_player = None
        elif web_mode == "lichess":
            self.game_mode = "OnlineHuman"
            self._web_player = None
        elif web_mode == "local":
            self.game_mode = "LocalHuman"
            self._web_player = None
        elif web_mode == "web_vs_ai":
            self.game_mode = "Stockfish"
            self._web_player = "White"
        elif web_mode == "web_local":
            self.game_mode = "LocalHuman"
            self._web_player = "both"

    def _setup_game(self):
        self.leds.control_panel_fill((255, 255, 255), start=0, count=4)
        self.leds.panel_show()

        if self.game_mode == "Stockfish":
            self.oled.show_setup_difficulty()
            self.display.show_difficulty_icon()
            web_server.update_state(setup_phase="difficulty", difficulty=CFG.difficulty_default)
            self._setup_preview_val = 1  
            self.oled.show_setup_difficulty(1)  
            ans = self._wait_setup_answer(phase="difficulty")
            ans_int = int(ans) if ans else CFG.difficulty_default
            if ans_int <= 8:
                self.difficulty = self._map_range(ans_int, 1, 8, CFG.difficulty_min, CFG.difficulty_max)
            else:
                self.difficulty = max(CFG.difficulty_min, min(CFG.difficulty_max, ans_int))
            self._setup_preview_val = 0
            self.oled.show_setup_difficulty(self.difficulty)
            web_server.update_state(difficulty=self.difficulty, setup_phase="time")

            self.oled.show_setup_timeout()
            self.display.show_timeout_icon()
            self._setup_preview_val = 5
            ans = self._wait_setup_answer(phase="time")
            TIME_MS = [1000, 2000, 3000, 5000, 8000, 12000, 20000, 30000]
            idx = max(1, min(8, int(ans) if ans else 5))
            self.move_timeout_ms = TIME_MS[idx - 1]
            self.oled.show_setup_timeout(self.move_timeout_ms)
            web_server.update_state(setup_phase="idle")

            if self._web_player:
                web_server.update_state(setup_phase="web_colour")
                ans = self._wait_setup_answer()
                self._web_player = str(ans) if ans else "White"
                web_server.update_state(setup_phase="idle", web_player=self._web_player)

        elif self.game_mode == "OnlineHuman":
            self.oled.show_setup_colour()
            self.display.show_colour_choice_icon()
            web_server.update_state(setup_phase="colour")
            ans = self._wait_setup_answer()
            self.colour_choice = ans if ans in ("white", "black") else "white"
            web_server.update_state(setup_phase="idle")
            self.lichess.start_game(self.colour_choice)

        elif self.game_mode == "LocalHuman":
            self.oled.show_local_game_start()
            if self._web_player == "both":
                web_server.update_state(setup_phase="idle")
            time.sleep(1.5)

        self.leds.control_panel_fill((10, 10, 10), start=6, count=16)
        self.leds.panel_show()

    def _ask_promotion(self, base_uci: str) -> str:
        colour = self._current_turn()
        self.display.show_promotion_choices()
        self.oled.show_promotion_select(colour)

        piece = "q"  
        while True:
            btn = self.buttons.detect_button()
            if btn in PROMOTION_MAP:
                piece = PROMOTION_MAP[btn]
                break

        self.anim.promotion_flash(piece)
        full_uci = self.board.promotion_move(base_uci, piece)
        return full_uci

    def _undo_sequence(self):
        if self.board.move_count() < 1:
            self.oled.show_status("Nothing to undo")
            return

        count = 2 if self.game_mode == "Stockfish" and self.board.move_count() >= 2 else 1
        undone = self.board.undo_half_moves(count)

        self.anim.undo_sweep()
        self.display.show_undo_icon()
        self.oled.show_undo_confirm(undone)
        web_server.update_state(
            fen=self.board.fen(), move_history=self.board._move_history_uci(), whose_turn=self._current_turn(),
            status=f"Undid {undone} half-move(s)", last_move=self.board._move_history_uci()[-1] if self.board._move_history_uci() else None,
        )
        time.sleep(1.5)
        self.anim.show_board_themed()
        self.suggested_best_move = ""

    def _end_game_draw(self, reason: str):
        self.oled.show_draw(reason)
        web_server.update_state(status=f"Draw — {reason}", game_active=False, draw_reason=reason)
        self.anim.rainbow_victory(duration=2.0)
        self._save_to_usb()

    def _end_game(self, winner: str):
        self.oled.show_checkmate(winner)
        web_server.update_state(status=f"Checkmate! {winner} wins!", game_active=False)
        self.anim.rainbow_victory(duration=4.0)
        threading.Thread(target=self._run_analysis, daemon=True).start()
        self._save_to_usb()

    def _run_analysis(self):
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
                loss = prev_score - score  
                if board.turn == _chess.WHITE:
                    loss = -loss  
                if loss > 100:
                    brd_before = _chess.Board(board.fen())
                    brd_before.pop()
                    _, best = self.stockfish.get_move(brd_before.fen(), skill_level=20, movetime_ms=200)
                    blunders.append({"move": uci, "loss": int(loss), "best": best or "?"})
                prev_score = score
            except Exception:
                pass

        blunders.sort(key=lambda x: x["loss"], reverse=True)
        top3 = blunders[:3]
        self.oled.show_analysis(top3)
        web_server.update_state(status=f"Analysis: {len(top3)} blunder(s) found")

    def _save_to_usb(self):
        if not usb.is_available():
            return
        filename = usb.save_game(
            move_history_uci=self.board._move_history_uci(),
            game_mode=self.game_mode or "Unknown",
            difficulty=self.difficulty,
        )
        if filename:
            self.oled.show_usb_saved(filename)
            web_server.update_state(status=f"Saved: {filename}")
            time.sleep(2)

    def _toggle_voice(self):
        if not voice.available:
            self.oled.show_status("No speaker detected")
            return
        self._voice_enabled = not self._voice_enabled
        state = "ON" if self._voice_enabled else "OFF"
        web_server.update_state(voice_enabled=self._voice_enabled)
        self.oled.show_status(f"Voice {state}")

    def _humans_go(self) -> str:
        current = self._current_turn()
        web_controls = (self._web_player in ("both", current))

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
                print(f"[SYSTEM] Clean clear. Button {btn} pressed instead of OK (9).")
                self.anim.show_board_themed()
                btn = 0

    def _get_coordinates(self, already_pressed: int = 0) -> str:
        col_map = {1: "a", 2: "b", 3: "c", 4: "d", 5: "e", 6: "f", 7: "g", 8: "h"}
        row_map = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8"}
        col_names = {1:"A", 2:"B", 3:"C", 4:"D", 5:"E", 6:"F", 7:"G", 8:"H"}

        column = None
        while column is None:
            btn = already_pressed if already_pressed != 0 else self.buttons.detect_button()
            already_pressed = 0
            column = col_map.get(btn)
            if column:
                self.oled.show_status(f"Column {col_names[btn]} — press row (1-8)")
        time.sleep(0.1)
        row = None
        while row is None:
            btn = self.buttons.detect_button()
            row = row_map.get(btn)
        time.sleep(0.1)
        return column + row

    def _web_move_received(self, uci: str):
        self._web_move_queue.append(uci)
        self._web_move_event.set()

    def _web_mode_received(self, mode: str):
        self._web_mode_queue.append(mode)
        self._web_mode_event.set()

    def _slider_preview(self, phase: str, value: int):
        self._setup_preview_val = value
        if phase == "difficulty":
            self.oled.show_setup_difficulty(value)
        elif phase == "time":
            TIME_OPTIONS = [1, 2, 3, 5, 8, 12, 20, 30]
            secs = TIME_OPTIONS[min(value - 1, 7)]
            self.oled.show_setup_timeout(secs * 1000)

    def _web_setup_received(self, value):
        self._web_setup_answer = value
        self._web_setup_event.set()

    def _wait_setup_answer(self, phase: str = "") -> Any:
        self._web_setup_event.clear()
        self._web_setup_answer = None

        while True:
            if self._web_setup_event.wait(timeout=0.03):
                self._web_setup_event.clear()
                return self._web_setup_answer

            btn = self.buttons.detect_button_nowait()
            if btn == 9:
                if not os.environ.get("MOCK_BUTTONS") == "1":
                    while self.buttons._scan_matrix() is not None:
                        time.sleep(0.01)
                    time.sleep(0.3)  
                return self._setup_preview_val or 4
            elif btn and btn != 0:
                if not os.environ.get("MOCK_BUTTONS") == "1":
                    while self.buttons._scan_matrix() is not None:
                        time.sleep(0.01)
                    time.sleep(0.3)  
                return btn
            time.sleep(0.02)

    def _disco_mode(self):
        import random
        self.anim.stop_thinking_animation()
        web_server.update_state(disco_active=True, status="DISCO MODE!")
        self.oled.show_status("DISCO MODE!")
        end = time.time() + 15.0
        while time.time() < end:
            for i in range(64):
                r = random.randint(0, 255)
                g = random.randint(0, 255)
                b = random.randint(0, 255)
                self.leds.chess_set_pixel(i % 8, i // 8, (r, g, b))
            self.leds.chess_show()
            time.sleep(0.05)
        web_server.update_state(disco_active=False, status="Your move")
        self.anim.show_board_themed()
        self.oled.show_game(self._current_turn(), status="Your move...", move_history=self.board._move_history_uci())

    def _check_move_legal(self, move_uci: str) -> bool:
        for _ in range(10):
            result = self.stockfish.is_legal(self.board.fen(), move_uci)
            if result is not None:
                return result
            time.sleep(0.05)
        return False

    def _hint_isr(self, channel):
        if self.buttons and self.buttons.is_button8_held():
            self._undo_sequence()
            return
        if self.buttons and self.buttons.is_ok_held():
            self._new_game_requested.set()
            return

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
            web_server.update_state(hint_move=self.suggested_best_move, status=f"Hint: {self.suggested_best_move}")
            self.anim.move_trail(self.suggested_best_move[:2], self.suggested_best_move[2:4])
            self.display.light_up_move(self.suggested_best_move, mode="H")
            self.anim.show_board_themed()
            self.leds.control_panel_set_pixel(5, (255, 255, 255))
            self.leds.control_panel_fill((255, 255, 255), start=0, count=4)
            self.leds.panel_show()
            self.oled.show_game(self._current_turn(), status="Hint shown")
            web_server.update_state(hint_move="", status="Your move")

    def _new_game_sequence(self):
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
            fen=self.board.fen(), move_history=[], whose_turn="White",
            status="New game — select mode", last_move=None, hint_move="",
            game_active=False, usb_available=usb.is_available()
        )
        self._choose_game_mode()
        self._setup_game()
        self.anim.show_board_themed()
        self.oled.show_game("White", status="New game! Your move.")
        web_server.update_state(game_active=True, status="Game started", whose_turn="White")

    def _start_clock(self, colour: str):
        self._active_colour = colour
        self._clock_start   = time.time()

    def _stop_clock(self, colour: str):
        if self._clock_start is not None:
            self._clock[colour] += time.time() - self._clock_start
            self._clock_start   = None

    def _current_turn(self) -> str:
        b = _chess.Board(self.board.fen())
        return "White" if b.turn == _chess.WHITE else "Black"

    def _push_move_to_ui(self, uci: str, player: str):
        history    = self.board._move_history_uci()
        whose      = self._current_turn()
        eval_score = self.stockfish.evaluate(self.board.fen())
        self.oled.push_move(uci, whose, eval_score=eval_score)
        web_server.update_state(
            fen=self.board.fen(), move_history=history, whose_turn=whose,
            last_move=uci, status=f"{player}: {uci}", eval_score=eval_score
        )

    @staticmethod
    def _get_local_ip() -> str:
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
        return int((value - in_min) * (out_max - out_min) / (in_max - in_min) + out_min)

    def _shutdown(self, *args):
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