# =============================================================================
# ui/display.py
# Author : Richard Pu
# Created: 2026-06-10
# Purpose: LED animations and board display logic for the smart chessboard.
#          Ports all visual functions from ArdunioChess.ino with exact timing
#          and pixel addressing. Game-mode confirmation flashes now target the
#          full 64-LED chessboard (green = AI, blue = online) rather than the
#          control-panel buttons, which are currently disabled.
# =============================================================================

import time
import logging
from hardware.leds import LEDController
from chess_engine.board_state import BoardState

log = logging.getLogger(__name__)

BLACK    = (0,   0,   0)
WHITE    = (255, 255, 255)
DIM_WHITE = (10, 10,  10)
RED      = (255, 0,   0)
GREEN    = (0,   255, 0)
BLUE     = (0,   0,   255)
CYAN     = (0,   255, 255)
MAGENTA  = (255, 0,   255)
YELLOW   = (255, 255, 0)
ORANGE   = (255, 165, 0)


class Display:
    def __init__(self, leds: LEDController, board: BoardState, buttons=None):
        self.leds    = leds
        self.board   = board
        self.buttons = buttons

    # ── Board markings ─────────────────────────────────────────────────────

    def show_board_markings(self):
        for col in range(0, 8, 2):
            for row in range(8):
                if row % 2 == 0:
                    self.leds.chess_set_pixel(col,     row, WHITE)
                    self.leds.chess_set_pixel(col + 1, row, BLACK)
                else:
                    self.leds.chess_set_pixel(col,     row, BLACK)
                    self.leds.chess_set_pixel(col + 1, row, WHITE)
        self.leds.chess_show()

    def show_opening_markings(self):
        for col in range(0, 8, 2):
            for row in list(range(2)) + list(range(6, 8)):
                if row % 2 == 0:
                    self.leds.chess_set_pixel(col,     row, WHITE)
                    self.leds.chess_set_pixel(col + 1, row, BLACK)
                else:
                    self.leds.chess_set_pixel(col,     row, BLACK)
                    self.leds.chess_set_pixel(col + 1, row, WHITE)
        self.leds.chess_show()

    # ── Loading animations ─────────────────────────────────────────────────

    def loading_animation_slow(self):
        for i in range(64):
            row = i // 8
            col = i % 8
            self.leds.chess_set_pixel(col, row, GREEN)
            self.leds.chess_show()
            time.sleep(1.0)

    def loading_animation_fast(self):
        for i in range(64):
            row = i // 8
            col = i % 8
            self.leds.chess_set_pixel(col, row, GREEN)
            self.leds.chess_show()
            time.sleep(0.025)

    # ── Game-mode confirmation flashes (full chessboard) ──────────────────

    def confirm_ai_mode(self, flashes: int = 2):
        """Flash the entire 64-LED chessboard GREEN to confirm AI (Stockfish) mode."""
        for _ in range(flashes):
            self.leds.chess_fill(GREEN)
            self.leds.chess_show()
            time.sleep(0.5)
            self.leds.chess_fill(BLACK)
            self.leds.chess_show()
            time.sleep(0.5)

    def confirm_online_mode(self, flashes: int = 2):
        """Flash the entire 64-LED chessboard BLUE to confirm Online (Lichess) mode."""
        for _ in range(flashes):
            self.leds.chess_fill(BLUE)
            self.leds.chess_show()
            time.sleep(0.5)
            self.leds.chess_fill(BLACK)
            self.leds.chess_show()
            time.sleep(0.5)

    # ── Move lighting ──────────────────────────────────────────────────────

    def light_up_move(self, uci: str, mode: str = "Y"):
        """
        Illuminate source and destination squares for a UCI move.

        mode:
          'Y' — show and continue immediately
          'N' — wait for OK button (btn 9) before proceeding
          'H' — hint in CYAN, auto-dismiss after 4 seconds

        If the destination square is occupied a RED capture flash plays first.
        """
        if len(uci) < 4:
            log.warning(f"light_up_move: UCI too short: {uci!r}")
            return

        fc, fr, tc, tr = self.board.parse_uci(uci)

        src_color = CYAN if mode == "H" else GREEN
        self.leds.chess_set_pixel(fc, fr, src_color)

        if self.board.is_square_occupied(tc, tr):
            for _ in range(3):
                self.leds.chess_set_pixel(tc, tr, RED)
                self.leds.chess_show()
                time.sleep(0.2)
                self.leds.chess_set_pixel(tc, tr, BLACK)
                self.leds.chess_show()
                time.sleep(0.2)
            self.leds.chess_set_pixel(tc, tr, RED)
            self.leds.chess_show()
        else:
            dst_color = CYAN if mode == "H" else GREEN
            self.leds.chess_set_pixel(tc, tr, dst_color)
            self.leds.chess_show()

        if mode == "N":
            self.leds.control_panel_set_pixel(4, (255, 255, 255))
            self.leds.panel_show()
            if self.buttons:
                while self.buttons.detect_button() != 9:
                    time.sleep(0.001)
            else:
                time.sleep(4)
            self.leds.control_panel_set_pixel(4, (0, 0, 0))
            self.leds.panel_show()

        elif mode == "H":
            time.sleep(4.0)

    # ── Error animation ────────────────────────────────────────────────────

    def error_animation(self):
        """Blue fill with red X diagonal, repeated 3 times."""
        for _ in range(3):
            self.leds.chess_fill(BLUE)
            self.leds.chess_show()
            time.sleep(0.5)
            self.leds.chess_draw_line(0, 7, 7, 0, RED)
            self.leds.chess_draw_line(0, 0, 7, 7, RED)
            self.leds.chess_show()
            time.sleep(0.5)

    # ── Checkmate animation ────────────────────────────────────────────────

    def checkmate_animation(self, attacker_uci: str):
        """Concentric red rectangles spiralling inward, then attacker flash."""
        rects = [(0, 0, 8, 8), (1, 1, 6, 6), (2, 2, 4, 4), (3, 3, 2, 2)]
        for x, y, w, h in rects:
            self.leds.chess_draw_rect(x, y, w, h, RED)
            self.leds.chess_show()
            time.sleep(1.0)

        if len(attacker_uci) >= 4:
            ac = self.board.col_from_char(attacker_uci[2])
            ar = self.board.row_from_char(attacker_uci[3])
            for _ in range(5):
                self.show_board_markings()
                time.sleep(1.0)
                self.leds.chess_set_pixel(ac, ar, RED)
                self.leds.chess_show()
                time.sleep(1.0)

    # ── Setup icons ────────────────────────────────────────────────────────

    def show_difficulty_icon(self):
        """'L' shape in MAGENTA — shown during difficulty selection."""
        self.leds.chess_fill(BLACK)
        self.leds.chess_fast_vline(x=2, y=2, h=4, color=MAGENTA)
        self.leds.chess_fast_hline(x=2, y=6, w=4, color=MAGENTA)
        self.leds.chess_show()

    def show_timeout_icon(self):
        """'!' shape in MAGENTA — shown during timeout selection."""
        self.leds.chess_fill(BLACK)
        self.leds.chess_fast_vline(x=3, y=2, h=5, color=MAGENTA)
        self.leds.chess_fast_hline(x=2, y=2, w=3, color=MAGENTA)
        self.leds.chess_show()

    def show_colour_choice_icon(self):
        """Left half bright white (white pieces), right half dim (black pieces)."""
        self.leds.chess_fill(BLACK)
        for row in range(8):
            for col in range(4):
                self.leds.chess_set_pixel(col, row, WHITE)
            for col in range(4, 8):
                self.leds.chess_set_pixel(col, row, DIM_WHITE)
        self.leds.chess_show()