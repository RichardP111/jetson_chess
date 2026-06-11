#!/usr/bin/env python3
# =============================================================================
# main.py
# Author : Richard Pu
# Created: 2026-06-10
# Purpose: Entry point for the Jetson Orin Nano smart chessboard.
#          Coordinates hardware init, game-mode selection, the main game loop,
#          Stockfish / Lichess integration, web dashboard, and OLED display.
#          API keys and secrets are loaded from the .env file via dotenv.
# =============================================================================

import time
import logging
import sys
import signal
import threading

from dotenv import load_dotenv

from hardware.leds import LEDController
from hardware.buttons import ButtonController
from chess_engine.board_state import BoardState
from chess_engine.stockfish import StockfishEngine
from online.lichess import LichessClient
from ui.display import Display
from ui.animations import AnimationEngine
from oled.oled_display import OLEDDisplay
import web.server as web_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/tmp/chess.log"),
    ],
)
log = logging.getLogger("main")


class ChessGame:
    def __init__(self):
        log.info("Initialising Jetson Smart Chess Board v3...")

        load_dotenv()

        self.leds     = LEDController()
        self.buttons  = ButtonController()
        self.board    = BoardState()
        self.display  = Display(self.leds, self.board, self.buttons)
        self.anim     = AnimationEngine(self.leds, self.board)
        self.oled     = OLEDDisplay()
        self.stockfish = StockfishEngine()
        self.lichess   = LichessClient()

        self.game_mode            = None
        self.colour_choice        = None
        self.difficulty           = 10
        self.move_timeout_ms      = 5000
        self.suggested_best_move  = ""

        self._new_game_requested = threading.Event()

        web_server.register_callbacks(
            new_game=self._new_game_sequence,
            hint=lambda: self._hint_isr(None),
            set_theme=self.anim.set_theme,
        )
        web_server.start_server()

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
        for i in range(64):
            row = i // 8
            col = i % 8
            self.leds.chess_set_pixel(col, row, (0, 255, 0))
            self.leds.chess_show()
            self.oled.show_loading(i + 1, 64)
            time.sleep(1.0)

        self._choose_game_mode()
        self._setup_game()
        self.anim.show_board_themed()
        self.oled.show_game(self._current_turn(), status="Game ready!")
        web_server.update_state(game_active=True, status="Game started")

    def _game_loop(self):
        while True:
            if self._new_game_requested.is_set():
                raise _NewGameException()

            self.oled.show_game(self._current_turn(), status="Your move...")
            web_server.update_state(whose_turn=self._current_turn(),
                                    status="Your move")
            humans_move = self._humans_go()
            log.info(f"Human move entered: {humans_move}")

            self.anim.move_trail(humans_move[:2], humans_move[2:4])
            self.display.light_up_move(humans_move, mode="Y")

            legal = self._check_move_legal(humans_move)
            if not legal:
                log.warning("Illegal move")
                self.display.error_animation()
                self.anim.show_board_themed()
                self.oled.show_status("Illegal move — try again")
                continue

            self.board.apply_move(humans_move)
            self._push_move_to_ui(humans_move, "Human")

            import chess as _chess
            cb = _chess.Board(self.board.fen())
            if cb.is_check():
                king_sq = cb.king(cb.turn)
                self.anim.check_alert(king_sq % 8, 7 - (king_sq // 8))
                self.oled.show_status("CHECK!")
                web_server.update_state(status="CHECK!")

            self.anim.show_board_themed()

            if self.game_mode == "Stockfish":
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

                try:
                    import chess as _chess
                    import chess.engine as _engine
                    brd     = _chess.Board(self.board.fen())
                    eng_tmp = _chess.engine.SimpleEngine.popen_uci(
                        self.stockfish._engine._transport._proc.args[0]
                        if hasattr(self.stockfish._engine, "_transport")
                        else "/usr/games/stockfish"
                    )
                    info  = eng_tmp.analyse(brd, _chess.engine.Limit(time=0.05))
                    score = info["score"].white().score(mate_score=10000)
                    web_server.update_state(eval_score=score or 0)
                    eng_tmp.quit()
                except Exception:
                    pass

                self.anim.move_trail(engine_move[:2], engine_move[2:4])
                self.display.light_up_move(engine_move, mode="N")
                log.info(engine_move)
                self.board.apply_move(engine_move)
                self._push_move_to_ui(engine_move, "Computer")

                self.leds.control_panel_set_pixel(5, (255, 255, 255))
                self.leds.panel_show()

                self._check_for_checkmate(best_hint, engine_move)

            elif self.game_mode == "OnlineHuman":
                self.lichess.send_move(humans_move)
                self.oled.show_status("Waiting for opponent...")
                web_server.update_state(status="Waiting for opponent...")
                opponent_move = self.lichess.wait_for_move()
                log.info(f"Online opponent move: {opponent_move}")
                self.anim.move_trail(opponent_move[:2], opponent_move[2:4])
                self.display.light_up_move(opponent_move, mode="N")
                log.info(opponent_move)
                self.board.apply_move(opponent_move)
                self._push_move_to_ui(opponent_move, "Opponent")

            self.anim.show_board_themed()
            self.oled.show_game(
                self._current_turn(),
                last_move=(
                    engine_move
                    if self.game_mode == "Stockfish"
                    else opponent_move
                ),
                move_history=self.board._move_history_uci(),
            )

    # ── Setup ──────────────────────────────────────────────────────────────

    def _choose_game_mode(self):
        log.info("Waiting for game mode selection...")
        self.oled.show_mode_select()
        self.leds.control_panel_fill((255, 255, 255), start=0, count=5)
        self.leds.panel_show()

        while True:
            btn = self.buttons.detect_button()
            time.sleep(0.3)
            if btn == 1:
                self.game_mode = "Stockfish"
                self.display.confirm_ai_mode()
                break
            elif btn == 2:
                self.game_mode = "OnlineHuman"
                self.display.confirm_online_mode()
                break

        web_server.update_state(game_mode=self.game_mode)
        log.info(f"Mode: {self.game_mode}")

    def _setup_game(self):
        self.leds.control_panel_fill((255, 255, 255), start=0, count=4)
        self.leds.panel_show()

        if self.game_mode == "Stockfish":
            self.oled.show_setup_difficulty()
            self.display.show_difficulty_icon()
            btn = self.buttons.detect_button()
            self.difficulty = self._map_range(btn, 1, 8, 1, 20)
            self.oled.show_setup_difficulty(self.difficulty)
            web_server.update_state(difficulty=self.difficulty)
            time.sleep(0.3)

            self.oled.show_setup_timeout()
            self.display.show_timeout_icon()
            btn = self.buttons.detect_button()
            self.move_timeout_ms = self._map_range(btn, 1, 8, 3000, 12000)
            self.oled.show_setup_timeout(self.move_timeout_ms)
            time.sleep(0.3)

        elif self.game_mode == "OnlineHuman":
            self.oled.show_setup_colour()
            self.display.show_colour_choice_icon()
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

        self.leds.control_panel_fill((10, 10, 10), start=6, count=16)
        self.leds.panel_show()

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
                btn = self.buttons.detect_button()
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
            btn = (already_pressed if already_pressed != 0
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
            log.info("Checkmate!")
            winner = "Black" if self._current_turn() == "White" else "White"
            self.oled.show_checkmate(winner)
            web_server.update_state(status=f"Checkmate! {winner} wins!")
            self.anim.rainbow_victory(duration=4.0)
            self.display.checkmate_animation(attacker_move)

    # ── Hint ISR ───────────────────────────────────────────────────────────

    def _hint_isr(self, channel):
        if self.buttons and self.buttons.is_ok_held():
            self._new_game_requested.set()
            return
        if self.buttons and self.buttons.is_button8_held():
            self._shutdown()
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
            self.display.light_up_move(self.suggested_best_move, mode="H")
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
        )
        self._setup_game()

    # ── Helpers ────────────────────────────────────────────────────────────

    def _current_turn(self) -> str:
        import chess as _chess
        b = _chess.Board(self.board.fen())
        return "White" if b.turn == _chess.WHITE else "Black"

    def _push_move_to_ui(self, uci: str, player: str):
        history = self.board._move_history_uci()
        whose   = self._current_turn()
        self.oled.push_move(uci, whose)
        web_server.update_state(
            fen=self.board.fen(),
            move_history=history,
            whose_turn=whose,
            last_move=uci,
            status=f"{player}: {uci}",
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