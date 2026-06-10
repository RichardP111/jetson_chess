"""
hardware/leds.py
WS2812b LED control for Jetson Orin Nano.

Two separate strips:
  - chessboard_leds : 64 pixels on GPIO pin 32 (PWM-capable, channel 0)
  - control_panel_leds : 22 pixels on GPIO pin 33 (PWM-capable, channel 1)

IMPORTANT hardware notes:
  - WS2812b data lines require 5V logic; Jetson GPIO is 3.3V.
    Use a 74AHCT125 level shifter on both data lines.
  - rpi_ws281x must be run as root OR with the udev rule in scripts/99-ws281x.rules.
  - If running on the desktop for development, set MOCK_LEDS=1 in your environment
    and this module will log LED calls instead of driving hardware.
"""

import os
import logging
from typing import Tuple

log = logging.getLogger(__name__)

MOCK = os.environ.get("MOCK_LEDS", "0") == "1"

# ── Pin / strip configuration ─────────────────────────────────────────────────
CHESS_LED_PIN = 32  # GPIO BCM pin for chessboard strip
CHESS_LED_COUNT = 64
CHESS_LED_BRIGHTNESS = 20  # 0-255  (matches original sketch)
CHESS_LED_CHANNEL = 0

PANEL_LED_PIN = 33  # GPIO BCM pin for control panel strip
PANEL_LED_COUNT = 22
PANEL_LED_BRIGHTNESS = 100
PANEL_LED_CHANNEL = 1

LED_FREQ_HZ = 800_000
LED_DMA = 10
LED_INVERT = False  # True only if using NPN transistor level shift

# ── Optional mock for desktop dev ─────────────────────────────────────────────
if not MOCK:
    try:
        from rpi_ws281x import PixelStrip, Color
    except ImportError:
        log.warning(
            "rpi_ws281x not found — falling back to mock mode. "
            "Install with: pip install rpi_ws281x"
        )
        MOCK = True

if MOCK:

    class Color:  # noqa: F811
        def __new__(cls, r, g, b):  # type: ignore[override]
            return (r << 16) | (g << 8) | b

    class PixelStrip:  # noqa: F811
        def __init__(
            self,
            count,
            pin,
            freq=800000,
            dma=10,
            invert=False,
            brightness=255,
            channel=0,
        ):
            self._count = count
            self._pixels = [0] * count
            self._brightness = brightness
            log.debug(f"[MOCK] PixelStrip(count={count}, pin={pin}, ch={channel})")

        def begin(self):
            log.debug("[MOCK] strip.begin()")

        def show(self):
            log.debug(f"[MOCK] strip.show() — pixels={self._pixels[:8]}...")

        def setPixelColor(self, n, color):
            if 0 <= n < self._count:
                self._pixels[n] = color

        def getPixelColor(self, n):
            return self._pixels[n] if 0 <= n < self._count else 0

        def numPixels(self):
            return self._count

        def setBrightness(self, brightness):
            self._brightness = brightness

        def fill(self, color, first=0, count=0):
            end = (first + count) if count else self._count
            for i in range(first, min(end, self._count)):
                self._pixels[i] = color


def _rgb(r: int, g: int, b: int) -> int:
    """Convert RGB tuple to rpi_ws281x Color int."""
    return Color(r, g, b)


class LEDController:
    """
    Unified controller for both WS2812b strips.

    Chessboard coordinates use (col, row) where col=0..7 (a..h) and row=0..7.
    The strip uses a ZIGZAG layout matching the original Adafruit_NeoMatrix config:
      even rows  → left to right  (col 0..7)
      odd rows   → right to left  (col 7..0)
    """

    def __init__(self):
        self.chess = PixelStrip(
            CHESS_LED_COUNT,
            CHESS_LED_PIN,
            LED_FREQ_HZ,
            LED_DMA,
            LED_INVERT,
            CHESS_LED_BRIGHTNESS,
            CHESS_LED_CHANNEL,
        )
        self.panel = PixelStrip(
            PANEL_LED_COUNT,
            PANEL_LED_PIN,
            LED_FREQ_HZ,
            LED_DMA,
            LED_INVERT,
            PANEL_LED_BRIGHTNESS,
            PANEL_LED_CHANNEL,
        )
        self.chess.begin()
        self.panel.begin()
        self.all_off()
        log.info("LEDController ready")

    # ── Chessboard helpers ────────────────────────────────────────────────────

    def _square_to_index(self, col: int, row: int) -> int:
        """Convert (col, row) grid coords to strip pixel index (zigzag layout)."""
        if row % 2 == 0:
            return row * 8 + col
        else:
            return row * 8 + (7 - col)

    def chess_set_pixel(self, col: int, row: int, color: Tuple[int, int, int]):
        idx = self._square_to_index(col, row)
        self.chess.setPixelColor(idx, _rgb(*color))

    def chess_fill(self, color: Tuple[int, int, int], start: int = 0, count: int = 0):
        c = _rgb(*color)
        end = (start + count) if count else CHESS_LED_COUNT
        for i in range(start, min(end, CHESS_LED_COUNT)):
            self.chess.setPixelColor(i, c)

    def chess_fill_rect(
        self, x: int, y: int, w: int, h: int, color: Tuple[int, int, int]
    ):
        for row in range(y, y + h):
            for col in range(x, x + w):
                self.chess_set_pixel(col, row, color)

    def chess_draw_rect(
        self, x: int, y: int, w: int, h: int, color: Tuple[int, int, int]
    ):
        """Draw rectangle outline (mirrors Adafruit drawRect)."""
        for col in range(x, x + w):
            self.chess_set_pixel(col, y, color)
            self.chess_set_pixel(col, y + h - 1, color)
        for row in range(y, y + h):
            self.chess_set_pixel(x, row, color)
            self.chess_set_pixel(x + w - 1, row, color)

    def chess_draw_line(
        self, x0: int, y0: int, x1: int, y1: int, color: Tuple[int, int, int]
    ):
        """Bresenham line draw (mirrors Adafruit drawLine)."""
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        while True:
            self.chess_set_pixel(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def chess_fast_vline(self, x: int, y: int, h: int, color: Tuple[int, int, int]):
        for row in range(y, y + h):
            self.chess_set_pixel(x, row, color)

    def chess_fast_hline(self, x: int, y: int, w: int, color: Tuple[int, int, int]):
        for col in range(x, x + w):
            self.chess_set_pixel(col, y, color)

    def chess_show(self):
        self.chess.show()

    # ── Control panel helpers ─────────────────────────────────────────────────

    def control_panel_set_pixel(self, idx: int, color: Tuple[int, int, int]):
        self.panel.setPixelColor(idx, _rgb(*color))

    def control_panel_fill(
        self, color: Tuple[int, int, int], start: int = 0, count: int = 0
    ):
        c = _rgb(*color)
        end = (start + count) if count else PANEL_LED_COUNT
        for i in range(start, min(end, PANEL_LED_COUNT)):
            self.panel.setPixelColor(i, c)

    def panel_show(self):
        self.panel.show()

    # ── Combined show ─────────────────────────────────────────────────────────

    def show_all(self):
        self.chess.show()
        self.panel.show()

    def all_off(self):
        self.chess_fill((0, 0, 0))
        self.control_panel_fill((0, 0, 0))
        self.show_all()
