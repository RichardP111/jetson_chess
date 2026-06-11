# =============================================================================
# ui/animations.py
# Author : Richard Pu
# Created: 2026-06-10
# Purpose: Enhanced LED animations and themes for the chessboard strip.
#          Provides move trails, piece pulse, check alert, rainbow victory,
#          thinking spinner, board wipe, and Matrix rain effect. Five built-in
#          themes: classic, fire, ocean, forest, neon.
# =============================================================================

import time
import math
import logging
import threading
from typing import Tuple

from hardware.leds import LEDController
from chess_engine.board_state import BoardState

log = logging.getLogger(__name__)

Color = Tuple[int, int, int]

# ── Theme definitions ─────────────────────────────────────────────────────────
THEMES = {
    "classic": {
        "light": (255, 255, 255),
        "dark":  (0,   0,   0),
        "move":  (0,   255, 0),
        "hint":  (0,   255, 255),
    },
    "fire": {
        "light": (255, 140, 0),    # amber
        "dark":  (80,  10, 0),     # dark red
        "move":  (255, 255, 0),    # yellow
        "hint":  (255, 80,  0),    # orange
    },
    "ocean": {
        "light": (0,   80, 180),   # deep blue
        "dark":  (0,   20, 60),    # navy
        "move":  (0,   255, 200),  # cyan-green
        "hint":  (100, 200, 255),  # sky blue
    },
    "forest": {
        "light": (20,  80, 20),    # dark green
        "dark":  (5,   20, 5),     # near black green
        "move":  (180, 230, 100),  # lime
        "hint":  (100, 200, 100),  # mid green
    },
    "neon": {
        "light": (120, 0,  200),   # purple
        "dark":  (0,   0,  40),    # near black
        "move":  (0,   255, 150),  # electric green
        "hint":  (255, 0,  200),   # magenta
    },
}

DEFAULT_THEME = "classic"


class AnimationEngine:
    def __init__(self, leds: LEDController, board: BoardState):
        self.leds  = leds
        self.board = board
        self._theme_name = DEFAULT_THEME
        self._theme = THEMES[DEFAULT_THEME]
        self._stop_bg = threading.Event()
        self._bg_thread: threading.Thread | None = None

    # ── Theme ─────────────────────────────────────────────────────────────────

    def set_theme(self, name: str):
        if name not in THEMES:
            log.warning(f"Unknown theme {name!r}, using classic")
            name = "classic"
        self._theme_name = name
        self._theme = THEMES[name]
        log.info(f"LED theme: {name}")

    def get_theme_names(self) -> list:
        return list(THEMES.keys())

    def show_board_themed(self):
        """Render the chessboard pattern in the current theme."""
        light = self._theme["light"]
        dark  = self._theme["dark"]
        for col in range(0, 8, 2):
            for row in range(8):
                if row % 2 == 0:
                    self.leds.chess_set_pixel(col,     row, light)
                    self.leds.chess_set_pixel(col + 1, row, dark)
                else:
                    self.leds.chess_set_pixel(col,     row, dark)
                    self.leds.chess_set_pixel(col + 1, row, light)
        self.leds.chess_show()

    # ── Move trail ────────────────────────────────────────────────────────────

    def move_trail(self, from_uci_sq: str, to_uci_sq: str, steps: int = 8):
        """
        Draw a fading ghost trail between two squares.
        from_uci_sq and to_uci_sq are square names like 'e2', 'e4'.
        """
        fc = self.board.col_from_char(from_uci_sq[0])
        fr = self.board.row_from_char(from_uci_sq[1])
        tc = self.board.col_from_char(to_uci_sq[0])
        tr = self.board.row_from_char(to_uci_sq[1])

        move_color = self._theme["move"]

        for step in range(steps + 1):
            t = step / steps
            # Interpolate position
            ic = int(fc + (tc - fc) * t)
            ir = int(fr + (tr - fr) * t)
            # Fade brightness: full at destination, trail fades
            brightness = t
            r = int(move_color[0] * brightness)
            g = int(move_color[1] * brightness)
            b = int(move_color[2] * brightness)
            self.leds.chess_set_pixel(ic, ir, (r, g, b))
            self.leds.chess_show()
            time.sleep(0.03)

        time.sleep(0.1)

    # ── Piece pulse ───────────────────────────────────────────────────────────

    def piece_pulse(self, color: Color = None, cycles: int = 2, duration: float = 1.0):
        """
        Gently breathe all occupied squares.
        Great to show while waiting for the player.
        """
        if color is None:
            color = self._theme["move"]

        steps = 30
        delay = duration / (steps * 2 * cycles)

        for _ in range(cycles):
            for direction in [range(steps), range(steps, -1, -1)]:
                for i in direction:
                    factor = i / steps
                    r = int(color[0] * factor)
                    g = int(color[1] * factor)
                    b = int(color[2] * factor)
                    for row in range(8):
                        for col in range(8):
                            if self.board.is_square_occupied(col, row):
                                self.leds.chess_set_pixel(col, row, (r, g, b))
                    self.leds.chess_show()
                    time.sleep(delay)

    # ── Check alert ───────────────────────────────────────────────────────────

    def check_alert(self, king_col: int, king_row: int, pulses: int = 4):
        """
        Rapid red pulse centred on the king square when check is detected.
        """
        for _ in range(pulses):
            # Concentric brightening
            for radius in range(0, 4):
                for dr in range(-radius, radius + 1):
                    for dc in range(-radius, radius + 1):
                        r, c = king_row + dr, king_col + dc
                        if 0 <= r < 8 and 0 <= c < 8:
                            brightness = max(0, 255 - radius * 60)
                            self.leds.chess_set_pixel(c, r, (brightness, 0, 0))
            self.leds.chess_show()
            time.sleep(0.12)
            # Show board again
            self.show_board_themed()
            time.sleep(0.08)

    # ── Rainbow victory ───────────────────────────────────────────────────────

    def rainbow_victory(self, duration: float = 4.0):
        """Full board rainbow sweep — shown on checkmate."""
        steps = 64
        delay = duration / steps

        def hsv_to_rgb(h: float) -> Color:
            """h in 0-1, returns (r,g,b) 0-255."""
            h6 = h * 6
            i = int(h6)
            f = h6 - i
            q = int(255 * (1 - f))
            t = int(255 * f)
            cycle = i % 6
            if cycle == 0: return (255, t,   0)
            if cycle == 1: return (q,   255, 0)
            if cycle == 2: return (0,   255, t)
            if cycle == 3: return (0,   q,   255)
            if cycle == 4: return (t,   0,   255)
            return (255, 0, q)

        for frame in range(steps):
            for row in range(8):
                for col in range(8):
                    hue = ((frame + row * 8 + col) % steps) / steps
                    self.leds.chess_set_pixel(col, row, hsv_to_rgb(hue))
            self.leds.chess_show()
            time.sleep(delay)

    # ── Thinking spinner ──────────────────────────────────────────────────────

    def start_thinking_animation(self, color: Color = (0, 100, 255)):
        """
        Non-blocking: starts a rotating dot around the board edge while
        the engine is thinking. Call stop_thinking_animation() when done.
        """
        self._stop_bg.clear()
        self._bg_thread = threading.Thread(
            target=self._spinner_loop, args=(color,), daemon=True
        )
        self._bg_thread.start()

    def stop_thinking_animation(self):
        self._stop_bg.set()
        if self._bg_thread:
            self._bg_thread.join(timeout=1.0)

    def _spinner_loop(self, color: Color):
        # Build the perimeter path (clockwise: top, right, bottom, left)
        path = (
            [(c, 0) for c in range(8)] +
            [(7, r) for r in range(1, 8)] +
            [(c, 7) for c in range(6, -1, -1)] +
            [(0, r) for r in range(6, 0, -1)]
        )
        tail = 4  # length of fading tail
        pos = 0
        while not self._stop_bg.is_set():
            for i, (col, row) in enumerate(path):
                # Restore
                self.leds.chess_set_pixel(col, row, self._square_color(col, row))
            for t in range(tail):
                idx = (pos - t) % len(path)
                col, row = path[idx]
                brightness = (tail - t) / tail
                r = int(color[0] * brightness)
                g = int(color[1] * brightness)
                b = int(color[2] * brightness)
                self.leds.chess_set_pixel(col, row, (r, g, b))
            self.leds.chess_show()
            pos = (pos + 1) % len(path)
            time.sleep(0.05)

    def _square_color(self, col: int, row: int) -> Color:
        """Return the themed colour for a square."""
        is_light = (col + row) % 2 == 0
        return self._theme["light"] if is_light else self._theme["dark"]

    # ── Board wipe transition ─────────────────────────────────────────────────

    def board_wipe(self, color: Color = (0, 0, 0), direction: str = "left"):
        """
        Wipe the board with a colour sweep.
        direction: 'left', 'right', 'top', 'bottom'
        """
        if direction == "left":
            for col in range(8):
                for row in range(8):
                    self.leds.chess_set_pixel(col, row, color)
                self.leds.chess_show()
                time.sleep(0.04)
        elif direction == "right":
            for col in range(7, -1, -1):
                for row in range(8):
                    self.leds.chess_set_pixel(col, row, color)
                self.leds.chess_show()
                time.sleep(0.04)
        elif direction == "top":
            for row in range(8):
                for col in range(8):
                    self.leds.chess_set_pixel(col, row, color)
                self.leds.chess_show()
                time.sleep(0.04)
        elif direction == "bottom":
            for row in range(7, -1, -1):
                for col in range(8):
                    self.leds.chess_set_pixel(col, row, color)
                self.leds.chess_show()
                time.sleep(0.04)

    # ── Rain effect ───────────────────────────────────────────────────────────

    def rain_effect(self, duration: float = 3.0, color: Color = (0, 200, 0)):
        """Cascading columns, Matrix-style. Good for startup."""
        import random
        drops = [random.randint(0, 9) for _ in range(8)]
        end = time.time() + duration

        while time.time() < end:
            self.leds.chess_fill((0, 0, 0))
            for col in range(8):
                row = drops[col] % 10
                if row < 8:
                    # Bright head
                    self.leds.chess_set_pixel(col, row, color)
                    # Fading tail
                    for t in range(1, 4):
                        tail_row = row - t
                        if 0 <= tail_row < 8:
                            factor = (4 - t) / 4
                            r = int(color[0] * factor * 0.4)
                            g = int(color[1] * factor * 0.4)
                            b = int(color[2] * factor * 0.4)
                            self.leds.chess_set_pixel(col, tail_row, (r, g, b))
                drops[col] = (drops[col] + 1) % 16
            self.leds.chess_show()
            time.sleep(0.07)
