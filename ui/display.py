"""
ui/display.py
All LED animations and board display logic.

Every visual function from ArdunioChess.ino is ported here with exact
timing and pixel addressing. The ButtonController is injected so that
mode='N' (wait for OK) can do a real button poll rather than a sleep.

Colors are (R, G, B) tuples throughout.
"""

import time
import logging
from hardware.leds import LEDController
from chess_engine.board_state import BoardState

log = logging.getLogger(__name__)

# ── Colour palette (mirrors Arduino #define constants) ────────────────────────
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
DIM_WHITE = (10, 10, 10)
RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
CYAN = (0, 255, 255)
MAGENTA = (255, 0, 255)
YELLOW = (255, 255, 0)
ORANGE = (255, 165, 0)


class Display:
    def __init__(self, leds: LEDController, board: BoardState, buttons=None):
        """
        buttons: ButtonController instance — required for mode='N' OK-wait.
        Pass None only in unit-test contexts where that code path won't be hit.
        """
        self.leds = leds
        self.board = board
        self.buttons = buttons

    # ── Board markings ────────────────────────────────────────────────────────

    def show_board_markings(self):
        """
        Alternating white/black pattern.
        Mirrors showChessboardMarkings() exactly:
          for i in 0,2,4,6:
            for j in 0..7:
              if j%2==0: pixel(i,j)=WHITE, pixel(i+1,j)=BLACK
              else:      pixel(i,j)=BLACK, pixel(i+1,j)=WHITE
        Note: Arduino uses (col, row) = (i, j) with i stepping by 2.
        """
        for col in range(0, 8, 2):
            for row in range(8):
                if row % 2 == 0:
                    self.leds.chess_set_pixel(col, row, WHITE)
                    self.leds.chess_set_pixel(col + 1, row, BLACK)
                else:
                    self.leds.chess_set_pixel(col, row, BLACK)
                    self.leds.chess_set_pixel(col + 1, row, WHITE)
        self.leds.chess_show()

    def show_opening_markings(self):
        """
        White/black pattern only on rows 0-1 and 6-7 (rank 8,7 and rank 2,1).
        Mirrors showChessboardOpeningMarkings().
        """
        for col in range(0, 8, 2):
            for row in list(range(2)) + list(range(6, 8)):
                if row % 2 == 0:
                    self.leds.chess_set_pixel(col, row, WHITE)
                    self.leds.chess_set_pixel(col + 1, row, BLACK)
                else:
                    self.leds.chess_set_pixel(col, row, BLACK)
                    self.leds.chess_set_pixel(col + 1, row, WHITE)
        self.leds.chess_show()

    # ── Loading animations ────────────────────────────────────────────────────

    def loading_animation_slow(self):
        """
        Lights one square per second up to 64.
        Mirrors waitForPiToStart() loadingStatus() loop with delay(1000).
        In the original this ran while polling for serial — here we just
        do it once through as a startup animation before mode select.
        """
        for i in range(64):
            row = i // 8
            col = i % 8
            self.leds.chess_set_pixel(col, row, GREEN)
            self.leds.chess_show()
            time.sleep(1.0)

    def loading_animation_fast(self):
        """
        25 ms per square — used in new-game reset.
        Mirrors the fast loadingStatus() loop in hint() new game block:
          while (var1 < 64) { var1 = loadingStatus(var1); delay(25); }
        """
        for i in range(64):
            row = i // 8
            col = i % 8
            self.leds.chess_set_pixel(col, row, GREEN)
            self.leds.chess_show()
            time.sleep(0.025)

    # ── Move lighting ─────────────────────────────────────────────────────────

    def light_up_move(self, uci: str, mode: str = "Y"):
        """
        Full port of lightUpMove(moveToUpdate, typeOfLight).

        mode:
          'Y' — auto continue (show, no wait)
          'N' — wait for OK button (btn 9) — lights OK panel LED, polls buttons
          'H' — hint in CYAN, auto-dismiss after 4 seconds

        Capture detection: if destination square is occupied → flash RED 3×
        else solid GREEN (or CYAN for hint).
        """
        if len(uci) < 4:
            log.warning(f"light_up_move: UCI too short: {uci!r}")
            return

        fc, fr, tc, tr = self.board.parse_uci(uci)

        # Source square colour
        src_color = CYAN if mode == "H" else GREEN
        self.leds.chess_set_pixel(fc, fr, src_color)

        # Destination square
        if self.board.is_square_occupied(tc, tr):
            # Capturing — flash RED 3× then hold RED
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

        # Mode-specific wait behaviour
        if mode == "N":
            # Light OK button, wait for btn 9
            self.leds.control_panel_set_pixel(4, (255, 255, 255))
            self.leds.panel_show()
            if self.buttons:
                while self.buttons.detect_button() != 9:
                    time.sleep(0.001)
            else:
                time.sleep(4)  # fallback if no buttons injected
            # Turn off OK button
            self.leds.control_panel_set_pixel(4, (0, 0, 0))
            self.leds.panel_show()

        elif mode == "H":
            time.sleep(4.0)  # 4 second auto-dismiss

        # mode 'Y' — no delay

    # ── Error animation ───────────────────────────────────────────────────────

    def error_animation(self):
        """
        Blue fill + red X (two diagonal lines). 3 times.
        Mirrors errorFromPi() exactly.
        """
        for _ in range(3):
            self.leds.chess_fill(BLUE)
            self.leds.chess_show()
            time.sleep(0.5)
            # drawLine(0,7,7,0) + drawLine(0,0,7,7)
            self.leds.chess_draw_line(0, 7, 7, 0, RED)
            self.leds.chess_draw_line(0, 0, 7, 7, RED)
            self.leds.chess_show()
            time.sleep(0.5)

    # ── Checkmate animation ───────────────────────────────────────────────────

    def checkmate_animation(self, attacker_uci: str):
        """
        Mirrors checkForComputerCheckMate() exactly:
          drawRect(0,0,8,8,RED) → delay 1s
          drawRect(1,1,6,6,RED) → delay 1s
          drawRect(2,2,4,4,RED) → delay 1s
          drawRect(3,3,2,2,RED) → delay 1s
          then 5× { showMarkings, delay 1s, attacker pixel RED, delay 1s }
        """
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

    # ── Setup icons ───────────────────────────────────────────────────────────

    def show_difficulty_icon(self):
        """
        'L' shape in MAGENTA on the chessboard.
        Mirrors setUpGame() Stockfish difficulty block:
          chessboardLEDS.fill(BLACK, 0)
          chessboardLEDS.drawFastVLine(2, 2, 4, MAGENTA)  ← vertical bar of L
          chessboardLEDS.drawFastHLine(2, 6, 4, MAGENTA)  ← base of L
        """
        self.leds.chess_fill(BLACK)
        self.leds.chess_fast_vline(x=2, y=2, h=4, color=MAGENTA)
        self.leds.chess_fast_hline(x=2, y=6, w=4, color=MAGENTA)
        self.leds.chess_show()

    def show_timeout_icon(self):
        """
        '!' (exclamation) shape in MAGENTA.
        Mirrors setUpGame() timeout block:
          chessboardLEDS.fill(BLACK, 0)
          chessboardLEDS.drawFastVLine(3, 2, 5, MAGENTA)  ← shaft
          chessboardLEDS.drawFastHLine(2, 2, 3, MAGENTA)  ← top bar
        """
        self.leds.chess_fill(BLACK)
        self.leds.chess_fast_vline(x=3, y=2, h=5, color=MAGENTA)
        self.leds.chess_fast_hline(x=2, y=2, w=3, color=MAGENTA)
        self.leds.chess_show()

    def show_colour_choice_icon(self):
        """
        Left half bright white = white, right half dim = black.
        Prompts player to press btn 1 (white) or btn 2 (black).
        """
        self.leds.chess_fill(BLACK)
        for row in range(8):
            for col in range(4):
                self.leds.chess_set_pixel(col, row, WHITE)
            for col in range(4, 8):
                self.leds.chess_set_pixel(col, row, DIM_WHITE)
        self.leds.chess_show()
