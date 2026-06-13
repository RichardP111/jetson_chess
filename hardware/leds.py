# =============================================================================
# hardware/leds.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: WS2812b LED control for the Jetson Orin Nano smart chessboard.
#          Uses Adafruit Blinka via SPI to bypass Jetson OS timing limitations.
#          The chessboard strip brightness is capped at ~30% (0.3)
#          to prevent overcurrent damage to the micro-USB connector.
#          Control panel strip is disabled pending hardware availability.
# Env   : Set MOCK_LEDS=1 to run without physical hardware (logs calls only).
# =============================================================================

import os
import logging
from typing import Any, Tuple, cast

from config import CFG

log = logging.getLogger(__name__)

MOCK = os.environ.get("MOCK_LEDS", "0") == "1"

if not MOCK:
    try:
        import board
        import neopixel_spi as neopixel
    except ImportError:
        log.warning(
            "neopixel_spi not found — falling back to mock mode. "
            "Install with: pip install adafruit-circuitpython-neopixel-spi Adafruit-Blinka"
        )
        MOCK = True

if MOCK:
    class MockNeoPixel:
        def __init__(self, pin, n, brightness=1.0, auto_write=True, pixel_order=None):
            self.n = n
            self.brightness = brightness
            self.pixels = [(0, 0, 0)] * n
            log.debug(f"[MOCK] NeoPixel(n={n}, pin={pin})")

        def __setitem__(self, index, val):
            if 0 <= index < self.n:
                self.pixels[index] = val

        def __getitem__(self, index):
            return self.pixels[index]

        def show(self):
            log.debug(f"[MOCK] strip.show() — pixels={self.pixels[:8]}...")
            
        def fill(self, color):
            self.pixels = [color] * self.n

    # Stub out the board and neopixel modules so the main class doesn't crash
    class _MockBoard:
        def SPI(self):
            return "MOCK_SPI"
            
    class _MockNeopixelMod:
        NeoPixel = MockNeoPixel
        NeoPixel_SPI = MockNeoPixel
        GRB = "GRB"
        
    board = _MockBoard()
    neopixel = _MockNeopixelMod()


class LEDController:
    """
    Unified controller for both WS2812b strips using Jetson SPI.

    Chessboard coordinates use (col, row): col 0-7 maps to files a-h,
    row 0-7 maps to ranks 1-8. Strip layout is zigzag:
      even rows → left to right  (col 0..7)
      odd rows  → right to left  (col 7..0)
    """

    def __init__(self):
        # Convert integer brightness (0-255) to a float (0.0-1.0) required by neopixel
        b_float = CFG.chess_led_brightness / 255.0
        
        self.chess = neopixel.NeoPixel_SPI(
            cast(Any, board.SPI()),
            CFG.chess_led_count,
            brightness=b_float,
            auto_write=False,
            pixel_order=neopixel.GRB
        )
        
        # Control panel strip is disabled — hardware not present.
        self.panel = None

        self.all_off()
        log.info("LEDController ready (Jetson SPI Mode)")

    # ── Chessboard helpers ─────────────────────────────────────────────────

    def _square_to_index(self, col: int, row: int) -> int:
        if row % 2 == 0:
            return row * 8 + col
        else:
            return row * 8 + (7 - col)

    def chess_set_pixel(self, col: int, row: int, color: Tuple[int, int, int]):
        idx = self._square_to_index(col, row)
        self.chess[idx] = color

    def chess_fill(self, color: Tuple[int, int, int], start: int = 0, count: int = 0):
        end = (start + count) if count else CFG.chess_led_count
        for i in range(start, min(end, CFG.chess_led_count)):
            self.chess[i] = color

    def chess_fill_rect(self, x: int, y: int, w: int, h: int, color: Tuple[int, int, int]):
        for row in range(y, y + h):
            for col in range(x, x + w):
                self.chess_set_pixel(col, row, color)

    def chess_draw_rect(self, x: int, y: int, w: int, h: int, color: Tuple[int, int, int]):
        for col in range(x, x + w):
            self.chess_set_pixel(col, y, color)
            self.chess_set_pixel(col, y + h - 1, color)
        for row in range(y, y + h):
            self.chess_set_pixel(x, row, color)
            self.chess_set_pixel(x + w - 1, row, color)

    def chess_draw_line(self, x0: int, y0: int, x1: int, y1: int, color: Tuple[int, int, int]):
        dx  = abs(x1 - x0)
        dy  = abs(y1 - y0)
        sx  = 1 if x0 < x1 else -1
        sy  = 1 if y0 < y1 else -1
        err = dx - dy
        while True:
            self.chess_set_pixel(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0  += sx
            if e2 < dx:
                err += dx
                y0  += sy

    def chess_fast_vline(self, x: int, y: int, h: int, color: Tuple[int, int, int]):
        for row in range(y, y + h):
            self.chess_set_pixel(x, row, color)

    def chess_fast_hline(self, x: int, y: int, w: int, color: Tuple[int, int, int]):
        for col in range(x, x + w):
            self.chess_set_pixel(col, y, color)

    def chess_show(self):
        self.chess.show()

    # ── Control panel helpers (no-ops while panel is disabled) ─────────────

    def control_panel_set_pixel(self, idx: int, color: Tuple[int, int, int]):
        if self.panel is not None:
            self.panel[idx] = color

    def control_panel_fill(self, color: Tuple[int, int, int], start: int = 0, count: int = 0):
        if self.panel is None:
            return
        end = (start + count) if count else CFG.panel_led_count
        for i in range(start, min(end, CFG.panel_led_count)):
            self.panel[i] = color

    def panel_show(self):
        if self.panel is not None:
            self.panel.show()

    # ── Combined show ──────────────────────────────────────────────────────

    def show_all(self):
        self.chess.show()
        if self.panel is not None:
            self.panel.show()

    def all_off(self):
        self.chess_fill((0, 0, 0))
        self.control_panel_fill((0, 0, 0))
        self.show_all()