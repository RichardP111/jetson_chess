import time
import logging
from hardware.leds import LEDController
from chess_engine.board_state import BoardState
from config import CFG

log = logging.getLogger(__name__)

BLACK     = (0,   0,   0)
WHITE     = (255, 255, 255)
DIM_WHITE = (10,  10,  10)
RED       = (255, 0,   0)
GREEN     = (0,   255, 0)
BLUE      = (0,   0,   255)
CYAN      = (0,   255, 255)
MAGENTA   = (255, 0,   255)
YELLOW    = (255, 255, 0)
ORANGE    = (255, 165, 0)


class Display:
    def __init__(self, leds: LEDController, board: BoardState, buttons=None):
        self.leds    = leds
        self.board   = board
        self.buttons = buttons

    # ── Board markings ─────────────────────────────────────────────────────

    def show_board_markings(self):
        for row in range(8):
            for col in range(8):
                colour = BLACK if (col + row) % 2 == 0 else WHITE
                self.leds.chess_set_pixel(col, row, colour)
        self.leds.chess_show()

    def show_opening_markings(self):
        for row in list(range(2)) + list(range(6, 8)):
            for col in range(8):
                colour = BLACK if (col + row) % 2 == 0 else WHITE
                self.leds.chess_set_pixel(col, row, colour)
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

    # ── Game-mode confirmation flashes ────────────────────────────────────

    def confirm_ai_mode(self, flashes: int = 2):
        """Flash full chessboard GREEN to confirm AI (Stockfish) mode."""
        for _ in range(flashes):
            self.leds.chess_fill(GREEN)
            self.leds.chess_show()
            time.sleep(0.5)
            self.leds.chess_fill(BLACK)
            self.leds.chess_show()
            time.sleep(0.5)

    def confirm_online_mode(self, flashes: int = 2):
        """Flash full chessboard BLUE to confirm Online (Lichess) mode."""
        for _ in range(flashes):
            self.leds.chess_fill(BLUE)
            self.leds.chess_show()
            time.sleep(0.5)
            self.leds.chess_fill(BLACK)
            self.leds.chess_show()
            time.sleep(0.5)

    def confirm_local_mode(self, flashes: int = 2):
        """Flash left-half white / right-half yellow to confirm local 2P mode."""
        for _ in range(flashes):
            for row in range(8):
                for col in range(4):
                    self.leds.chess_set_pixel(col, row, WHITE)
                for col in range(4, 8):
                    self.leds.chess_set_pixel(col, row, YELLOW)
            self.leds.chess_show()
            time.sleep(0.5)
            self.leds.chess_fill(BLACK)
            self.leds.chess_show()
            time.sleep(0.5)

    # ── Move lighting ──────────────────────────────────────────────────────

    def light_up_move(self, uci: str, mode: str = "Y"):
        """
        Illuminate source and destination squares for a UCI move.
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
            time.sleep(CFG.hint_dismiss_s)

    # ── Promotion display ──────────────────────────────────────────────────

    def show_promotion_choices(self):
        """
        Display four coloured quadrants for promotion piece selection
        """
        self.leds.chess_fill(BLACK)
        # Q — white — top-left 4×4
        for row in range(4):
            for col in range(4):
                self.leds.chess_set_pixel(col, row, WHITE)
        # R — red — top-right 4×4
        for row in range(4):
            for col in range(4, 8):
                self.leds.chess_set_pixel(col, row, RED)
        # B — blue — bottom-left 4×4
        for row in range(4, 8):
            for col in range(4):
                self.leds.chess_set_pixel(col, row, BLUE)
        # N — yellow — bottom-right 4×4
        for row in range(4, 8):
            for col in range(4, 8):
                self.leds.chess_set_pixel(col, row, YELLOW)
        self.leds.chess_show()

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
        """'L' shape in MAGENTA — Aligned to hardware row orientation."""
        self.leds.chess_fill(BLACK)
        self.leds.chess_fast_vline(x=2, y=1, h=5, color=MAGENTA)  # Stem from row 1 to 5
        self.leds.chess_fast_hline(x=2, y=1, w=4, color=MAGENTA)  # Bottom base at row 1
        self.leds.chess_show()

    def show_timeout_icon(self):
        """'T' shape in MAGENTA — Aligned to hardware row orientation."""
        self.leds.chess_fill(BLACK)
        self.leds.chess_fast_vline(x=3, y=1, h=5, color=MAGENTA)  # Stem from row 1 to 5
        self.leds.chess_fast_hline(x=2, y=5, w=3, color=MAGENTA)  # Top crossbar at row 5
        self.leds.chess_show()

    def show_colour_choice_icon(self):
        """Left half bright white, right half dim — white vs black side."""
        self.leds.chess_fill(BLACK)
        for row in range(8):
            for col in range(4):
                self.leds.chess_set_pixel(col, row, WHITE)
            for col in range(4, 8):
                self.leds.chess_set_pixel(col, row, DIM_WHITE)
        self.leds.chess_show()

    def show_undo_icon(self):
        """Orange left-arrow on the board — Aligned to hardware row center."""
        self.leds.chess_fill(BLACK)
        # Centered shaft on row 4
        for col in range(2, 7):
            self.leds.chess_set_pixel(col, 4, ORANGE)
        # Arrowhead point and wings
        self.leds.chess_set_pixel(1, 4, ORANGE)
        self.leds.chess_set_pixel(2, 3, ORANGE)
        self.leds.chess_set_pixel(2, 5, ORANGE)
        self.leds.chess_show()