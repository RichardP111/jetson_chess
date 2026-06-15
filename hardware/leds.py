# =============================================================================
# hardware/leds.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: WS2812b LED control for the Jetson Orin Nano smart chessboard.
#          Manages two LED strips — 64-pixel chessboard and 22-pixel control
#          panel. The chessboard strip brightness is capped at ~30 % (76/255)
#          to prevent overcurrent damage to the micro-USB connector.
#          Control panel strip is disabled pending hardware availability.
# Env   : Set MOCK_LEDS=1 to run without physical hardware (logs calls only).
# =============================================================================

import os
import logging
from typing import Any, Tuple

from config import CFG

log = logging.getLogger(__name__)

MOCK = os.environ.get("MOCK_LEDS", "0") == "1"

# ── Hardware strip implementation ─────────────────────────────────────────────
# Uses adafruit-circuitpython-neopixel-spi for SPI-based WS2812b on Jetson.
# The Jetson Orin Nano does not support rpi_ws281x DMA/PWM modes.
# LED data pin: BOARD 19 (SPI1 MOSI).

class _SpiStrip:
    """
    WS2812b driver using raw spidev — bypasses busio/blinka conflicts.
    Talks directly to /dev/spidev1.0 (SPI1, BOARD pin 19 = MOSI).

    WS2812b protocol encoded in SPI:
      Each bit is 3 SPI bits: 1=110, 0=100 at ~2.4 MHz → ~800 kHz LED signal.
    """
    # SPI bus/device — spidev0.0 confirmed working on BOARD pin 19
    SPI_BUS    = 0
    SPI_DEVICE = 0
    SPI_HZ     = 2_400_000  # 3 SPI bits per WS2812b bit → 800 kHz

    def __init__(self, count: int, brightness: int = 76):
        import spidev  # type: ignore[import-untyped]
        self._count      = count
        self._pixels     = [0] * count
        self._brightness = brightness / 255.0
        self._spi        = spidev.SpiDev()
        self._spi.open(self.SPI_BUS, self.SPI_DEVICE)
        self._spi.max_speed_hz = self.SPI_HZ
        self._spi.mode         = 0
        log.info(f"SPI strip on /dev/spidev{self.SPI_BUS}.{self.SPI_DEVICE} "
                 f"@ {self.SPI_HZ//1000} kHz, {count} pixels")

    def begin(self):
        pass

    def _encode_byte(self, byte: int) -> list:
        """Encode one byte into 3 SPI bytes (24 bits = 8 WS2812b bits)."""
        out = []
        for i in range(7, -1, -1):
            if (byte >> i) & 1:
                out.append(0b11000000)  # 1-bit: HIGH HIGH LOW
            else:
                out.append(0b10000000)  # 0-bit: HIGH LOW  LOW
        return out

    def _build_frame(self) -> bytes:
        """Build full SPI frame for all pixels."""
        buf = []
        brt = self._brightness
        for packed in self._pixels:
            r = int(((packed >> 16) & 0xFF) * brt)
            g = int(((packed >> 8)  & 0xFF) * brt)
            b = int(( packed        & 0xFF) * brt)
            # WS2812b order is GRB
            buf += self._encode_byte(g)
            buf += self._encode_byte(r)
            buf += self._encode_byte(b)
        # Reset pulse: ≥50 µs LOW = at least 15 zero bytes at 2.4 MHz
        buf += [0x00] * 20
        return bytes(buf)

    def show(self):
        frame = self._build_frame()
        # spidev writebytes2 handles large buffers
        self._spi.writebytes2(frame)

    def getPixelColor(self, n: int) -> int:
        return self._pixels[n] if 0 <= n < self._count else 0

    def setPixelColor(self, n: int, color: int) -> None:
        if 0 <= n < self._count:
            self._pixels[n] = color

    def numPixels(self) -> int:
        return self._count

    def setBrightness(self, brightness: int) -> None:
        self._brightness = max(0.004, brightness / 255.0)

    def fill(self, color: int, first: int = 0, count: int = 0) -> None:
        end = (first + count) if count else self._count
        for i in range(first, min(end, self._count)):
            self._pixels[i] = color


class _MockStrip:
    """Software-only strip for desktop testing (MOCK_LEDS=1)."""
    def __init__(self, count: int, brightness: int = 76):
        self._count      = count
        self._pixels     = [0] * count
        self._brightness = brightness
        log.debug(f"[MOCK] Strip(count={count})")

    def begin(self): pass

    def show(self):
        log.debug(f"[MOCK] show() pixels={self._pixels[:8]}...")

    def getPixelColor(self, n: int) -> int:
        return self._pixels[n] if 0 <= n < self._count else 0

    def setPixelColor(self, n: int, color: int) -> None:
        if 0 <= n < self._count:
            self._pixels[n] = color

    def numPixels(self) -> int:
        return self._count

    def setBrightness(self, brightness: int) -> None:
        self._brightness = brightness

    def fill(self, color: int, first: int = 0, count: int = 0) -> None:
        end = (first + count) if count else self._count
        for i in range(first, min(end, self._count)):
            self._pixels[i] = color


def _make_strip(count: int, brightness: int) -> Any:
    if MOCK:
        return _MockStrip(count, brightness)
    try:
        return _SpiStrip(count, brightness)
    except Exception as e:
        log.warning(f"SPI strip init failed ({e}) — using mock")
        return _MockStrip(count, brightness)


def _rgb(r: int, g: int, b: int) -> int:
    return (r << 16) | (g << 8) | b


class LEDController:
    """
    Unified controller for both WS2812b strips.

    Chessboard coordinates use (col, row): col 0-7 maps to files a-h,
    row 0-7 maps to ranks 1-8. Strip layout is zigzag:
      even rows → left to right  (col 0..7)
      odd rows  → right to left  (col 7..0)
    """

    def __init__(self):
        # SPI-based WS2812b — adafruit_neopixel_spi handles BOARD pin 19 (SPI MOSI)
        self.chess = _make_strip(CFG.chess_led_count, CFG.chess_led_brightness)
        self.chess.begin()

        # Control panel strip disabled — hardware not present.
        self.panel: Any = None

        self.all_off()
        log.info("LEDController ready")

    # ── Chessboard helpers ─────────────────────────────────────────────────

    def _square_to_index(self, col: int, row: int) -> int:
        # Strip enters at H1 (bottom-right corner), pixel 0.
        # row 0 = rank 1 = strip row 0 (no flip needed).
        # Even rows (rank 1,3,5,7): strip runs H→A so col 7 (H) = pixel 0 of row
        # Odd rows  (rank 2,4,6,8): strip runs A→H so col 0 (A) = pixel 0 of row
        phys_col = (7 - col) if row % 2 == 0 else col
        return row * 8 + phys_col

    def chess_set_pixel(self, col: int, row: int, color: Tuple[int, int, int]):
        idx = self._square_to_index(col, row)
        self.chess.setPixelColor(idx, _rgb(*color))

    def chess_fill(self, color: Tuple[int, int, int], start: int = 0, count: int = 0):
        c   = _rgb(*color)
        end = (start + count) if count else CFG.chess_led_count
        for i in range(start, min(end, CFG.chess_led_count)):
            self.chess.setPixelColor(i, c)

    def chess_fill_rect(self, x: int, y: int, w: int, h: int,
                        color: Tuple[int, int, int]):
        for row in range(y, y + h):
            for col in range(x, x + w):
                self.chess_set_pixel(col, row, color)

    def chess_draw_rect(self, x: int, y: int, w: int, h: int,
                        color: Tuple[int, int, int]):
        for col in range(x, x + w):
            self.chess_set_pixel(col, y, color)
            self.chess_set_pixel(col, y + h - 1, color)
        for row in range(y, y + h):
            self.chess_set_pixel(x, row, color)
            self.chess_set_pixel(x + w - 1, row, color)

    def chess_draw_line(self, x0: int, y0: int, x1: int, y1: int,
                        color: Tuple[int, int, int]):
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
        self._push_led_state()

    def _push_led_state(self):
        """
        Push current 8×8 LED colours to the web dashboard.
        Fixed: Replaced range with reversed(range) to match top-down HTML grids.
        """
        try:
            grid = []
            # Sweep from row 7 (top/Rank 8) down to row 0 (bottom/Rank 1)
            for row in reversed(range(8)):
                for col in range(8):
                    idx    = self._square_to_index(col, row)
                    packed = self.chess.getPixelColor(idx)
                    r = (packed >> 16) & 0xFF
                    g = (packed >> 8)  & 0xFF
                    b =  packed        & 0xFF
                    grid.append([r, g, b])
            # Lazy import avoids circular dependency at module load time
            import web.server as _web  # noqa: PLC0415
            if hasattr(_web, "update_led_grid"):
                _web.update_led_grid(grid)
        except Exception:
            pass

    # ── Control panel helpers (no-ops while panel is disabled) ─────────────

    def control_panel_set_pixel(self, idx: int, color: Tuple[int, int, int]):
        if self.panel is not None:
            self.panel.setPixelColor(idx, _rgb(*color))

    def control_panel_fill(self, color: Tuple[int, int, int],
                           start: int = 0, count: int = 0):
        if self.panel is None:
            return
        c   = _rgb(*color)
        end = (start + count) if count else CFG.panel_led_count
        for i in range(start, min(end, CFG.panel_led_count)):
            self.panel.setPixelColor(i, c)

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