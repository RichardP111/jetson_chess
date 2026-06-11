# =============================================================================
# oled/oled_display.py
# Author : Richard Pu
# Created: 2026-06-10
# Purpose: SSD1306 128×64 I2C OLED driver for the Grove connector on the
#          Jetson. Renders idle clock, mode select, setup, game, hint,
#          checkmate, and loading screens. Set MOCK_OLED=1 for headless use.
# =============================================================================

import os
import time
import logging
import threading
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

MOCK = os.environ.get("MOCK_OLED", "0") == "1"
I2C_PORT = int(os.environ.get("OLED_I2C_PORT", "1"))  # /dev/i2c-1 on Jetson
I2C_ADDR = int(os.environ.get("OLED_I2C_ADDR", "0x3C"), 16)
WIDTH, HEIGHT = 128, 64

if not MOCK:
    try:
        from luma.core.interface.serial import i2c
        from luma.oled.device import ssd1306
        from luma.core.render import canvas
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log.warning(
            "luma.oled / Pillow not found — OLED in mock mode. "
            "Install: pip install luma.oled Pillow --break-system-packages"
        )
        MOCK = True

if MOCK:
    # Minimal stubs so the rest of the code imports cleanly
    class _FakeDevice:
        width = WIDTH
        height = HEIGHT

        def display(self, img):
            pass

        def cleanup(self):
            pass

        def hide(self):
            pass

        def show(self):
            pass

    class _FakeCanvas:
        def __init__(self, device):
            self._dev = device

        def __enter__(self):
            return _FakeDraw()

        def __exit__(self, *a):
            pass

    class _FakeDraw:
        def rectangle(self, *a, **k):
            pass

        def text(self, *a, **k):
            pass

        def line(self, *a, **k):
            pass

        def ellipse(self, *a, **k):
            pass

    def _make_device():
        log.debug("[MOCK OLED] device created")
        return _FakeDevice()

    canvas = _FakeCanvas  # noqa: F811
    Image = None

    def _load_font(size=10):
        return None

else:

    def _make_device():
        serial = i2c(port=I2C_PORT, address=I2C_ADDR)
        return ssd1306(serial, width=WIDTH, height=HEIGHT)

    def _load_font(size=10):
        try:
            return ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size
            )
        except Exception:
            return ImageFont.load_default()


# ── Fonts (loaded once) ───────────────────────────────────────────────────────
_FONT_SMALL = None
_FONT_MED = None
_FONT_LARGE = None
_FONT_TINY = None


def _fonts():
    global _FONT_SMALL, _FONT_MED, _FONT_LARGE, _FONT_TINY
    if _FONT_SMALL is None:
        _FONT_SMALL = _load_font(10)
        _FONT_MED = _load_font(14)
        _FONT_LARGE = _load_font(20)
        _FONT_TINY = _load_font(8)
    return _FONT_SMALL, _FONT_MED, _FONT_LARGE, _FONT_TINY


class OLEDDisplay:
    """
    Rich OLED UI for the chess board.

    Screens:
      - idle        : clock + "Smart Chess" branding
      - mode_select : game mode prompt
      - setup       : difficulty / timeout / colour setup
      - game        : move history, whose turn, last move, status
      - hint        : suggested best move highlighted
      - checkmate   : winner announcement
      - new_game    : "New Game!" splash
    """

    def __init__(self):
        self._device = _make_device()
        self._lock = threading.Lock()
        self._current_screen = "idle"

        # Game state tracked for the game screen
        self._move_history: list[str] = []  # list of UCI strings in order
        self._whose_turn = "White"
        self._last_move = ""
        self._status_msg = ""
        self._game_mode = ""
        self._difficulty = 0
        self._hint_move = ""

        self._clock_thread: Optional[threading.Thread] = None
        self._clock_running = False

        log.info(f"OLEDDisplay ready (mock={MOCK}, addr=0x{I2C_ADDR:02X})")

    # ── Public API ────────────────────────────────────────────────────────────

    def show_idle(self):
        """Clock + branding. Starts a background thread updating every second."""
        self._current_screen = "idle"
        self._start_clock()

    def show_mode_select(self):
        """Prompt: 'Press 1: AI  Press 2: Online'."""
        self._current_screen = "mode_select"
        self._stop_clock()
        self._draw_mode_select()

    def show_setup_difficulty(self, current: int = 0):
        """Difficulty selection prompt with bar graph."""
        self._current_screen = "setup"
        self._stop_clock()
        self._draw_setup("Difficulty", "Press 1-8", current, 8)

    def show_setup_timeout(self, current_ms: int = 0):
        """Timeout selection prompt."""
        self._current_screen = "setup"
        secs = current_ms // 1000
        self._draw_setup("Move Time", "Press 1-8", secs, 12)

    def show_setup_colour(self):
        """Colour selection prompt."""
        self._current_screen = "setup"
        self._draw_colour_select()

    def show_game(
        self,
        whose_turn: str,
        last_move: str = "",
        move_history: list = None,
        status: str = "",
    ):
        """Main game screen: move history list, whose turn, status line."""
        self._current_screen = "game"
        self._stop_clock()
        self._whose_turn = whose_turn
        self._last_move = last_move
        self._status_msg = status
        if move_history is not None:
            self._move_history = move_history
        self._draw_game()

    def push_move(self, uci: str, whose_turn_after: str):
        """Add a move to history and refresh the game screen."""
        self._move_history.append(uci)
        self._last_move = uci
        self._whose_turn = whose_turn_after
        if self._current_screen == "game":
            self._draw_game()

    def show_hint(self, hint_uci: str):
        """Flash the hint move prominently."""
        self._hint_move = hint_uci
        self._draw_hint(hint_uci)

    def show_status(self, msg: str):
        """One-line status update on the game screen."""
        self._status_msg = msg
        if self._current_screen == "game":
            self._draw_game()

    def show_checkmate(self, winner: str):
        """Large CHECKMATE announcement."""
        self._current_screen = "checkmate"
        self._stop_clock()
        self._draw_checkmate(winner)

    def show_new_game(self):
        """'New Game!' splash for 2 seconds then go idle."""
        self._current_screen = "new_game"
        self._draw_new_game()

    def show_loading(self, progress: int, total: int = 64):
        """Loading bar — call repeatedly as board fills up."""
        self._draw_loading(progress, total)

    def clear(self):
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill="black")

    def cleanup(self):
        self._stop_clock()
        self.clear()
        self._device.cleanup()

    # ── Drawing routines ──────────────────────────────────────────────────────

    def _draw_mode_select(self):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill="black")
                # Title bar
                draw.rectangle((0, 0, WIDTH - 1, 14), fill="white")
                draw.text((4, 1), "Smart Chess Board", font=fs, fill="black")
                # Options
                draw.text((4, 18), "Btn 1:  vs AI (Stockfish)", font=fs, fill="white")
                draw.text((4, 30), "Btn 2:  vs Human (Lichess)", font=fs, fill="white")
                # Divider
                draw.line((0, 44, WIDTH - 1, 44), fill="white")
                draw.text((4, 47), "NVIDIA Jetson Orin Nano", font=ft, fill="white")
                draw.text((4, 56), "Smart Chess v2", font=ft, fill="white")

    def _draw_setup(self, label: str, prompt: str, current: int, max_val: int):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill="black")
                draw.rectangle((0, 0, WIDTH - 1, 14), fill="white")
                draw.text((4, 1), label, font=fm, fill="black")
                draw.text((4, 18), prompt, font=fs, fill="white")
                if current > 0:
                    bar_w = int((current / max_val) * (WIDTH - 8))
                    draw.rectangle((4, 34, 4 + bar_w, 44), fill="white")
                    draw.rectangle((4, 34, WIDTH - 4, 44), outline="white")
                    draw.text((4, 48), f"Current: {current}", font=fs, fill="white")
                else:
                    draw.rectangle((4, 34, WIDTH - 4, 44), outline="white")
                    draw.text((4, 48), "Waiting...", font=fs, fill="white")

    def _draw_colour_select(self):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill="black")
                draw.rectangle((0, 0, WIDTH - 1, 14), fill="white")
                draw.text((4, 1), "Choose Colour", font=fm, fill="black")
                # White side
                draw.rectangle((4, 20, 58, 50), fill="white")
                draw.text((14, 30), "WHITE", font=fs, fill="black")
                draw.text((14, 42), "Btn 1", font=ft, fill="black")
                # Black side
                draw.rectangle((66, 20, 122, 50), outline="white")
                draw.text((76, 30), "BLACK", font=fs, fill="white")
                draw.text((76, 42), "Btn 2", font=ft, fill="white")

    def _draw_game(self):
        fs, fm, fl, ft = _fonts()
        history = self._move_history

        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill="black")

                # ── Top bar: whose turn ───────────────────────────────────────
                bar_color = "white" if self._whose_turn == "White" else "gray"
                draw.rectangle((0, 0, WIDTH - 1, 13), fill=bar_color)
                turn_text = f"{self._whose_turn}'s Turn"
                tc = "black" if self._whose_turn == "White" else "white"
                draw.text((4, 1), turn_text, font=fs, fill=tc)

                # Mode badge (right side of bar)
                mode_lbl = "AI" if "Stockfish" in self._game_mode else "Online"
                draw.text((90, 1), mode_lbl, font=ft, fill=tc)

                # ── Move history (last 4 moves, 2 columns) ───────────────────
                y = 17
                draw.text((0, y), "Move History:", font=ft, fill="white")
                y += 9

                # Pair moves into (White, Black) rows
                pairs = []
                for i in range(0, len(history), 2):
                    w = history[i]
                    b = history[i + 1] if i + 1 < len(history) else "..."
                    pairs.append((i // 2 + 1, w, b))

                # Show last 3 pairs
                for num, white_m, black_m in pairs[-3:]:
                    draw.text((0, y), f"{num}.", font=ft, fill="white")
                    draw.text((14, y), white_m, font=ft, fill="white")
                    draw.text((60, y), black_m, font=ft, fill="white")
                    y += 9

                # ── Status bar ────────────────────────────────────────────────
                draw.line((0, HEIGHT - 12, WIDTH - 1, HEIGHT - 12), fill="white")
                status = self._status_msg or (
                    f"Last: {self._last_move}"
                    if self._last_move
                    else "Game in progress"
                )
                draw.text((2, HEIGHT - 10), status, font=ft, fill="white")

    def _draw_hint(self, uci: str):
        fs, fm, fl, ft = _fonts()
        # Convert UCI to human-readable e.g. "e2 → e4"
        if len(uci) >= 4:
            readable = f"{uci[:2].upper()} \u2192 {uci[2:4].upper()}"
        else:
            readable = uci

        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill="black")
                draw.rectangle((0, 0, WIDTH - 1, 14), fill="white")
                draw.text((4, 1), "Hint", font=fm, fill="black")
                # Large move text centred
                draw.text((14, 22), readable, font=fl, fill="white")
                draw.text((4, 50), "Press HINT again to dismiss", font=ft, fill="white")

    def _draw_checkmate(self, winner: str):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill="black")
                draw.rectangle((2, 2, WIDTH - 3, HEIGHT - 3), outline="white")
                draw.rectangle((5, 5, WIDTH - 6, HEIGHT - 6), outline="white")
                draw.text((8, 10), "CHECKMATE!", font=fm, fill="white")
                draw.text((8, 28), f"{winner} wins!", font=fs, fill="white")
                draw.text(
                    (8, 42), f"{len(self._move_history)} moves", font=ft, fill="white"
                )
                draw.text((8, 52), "Press OK for new game", font=ft, fill="white")

    def _draw_new_game(self):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill="black")
                draw.text((20, 18), "NEW GAME!", font=fm, fill="white")
                draw.text((30, 38), "Good luck!", font=fs, fill="white")

    def _draw_loading(self, progress: int, total: int = 64):
        fs, fm, fl, ft = _fonts()
        pct = int((progress / total) * 100)
        bar_w = int((progress / total) * (WIDTH - 8))

        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill="black")
                draw.text((4, 6), "Smart Chess Board", font=fs, fill="white")
                draw.text((4, 18), "NVIDIA Jetson Orin", font=ft, fill="white")
                # Progress bar
                draw.rectangle((4, 34, WIDTH - 4, 46), outline="white")
                if bar_w > 0:
                    draw.rectangle((4, 34, 4 + bar_w, 46), fill="white")
                draw.text((4, 50), f"Starting... {pct}%", font=ft, fill="white")

    def _draw_clock(self):
        fs, fm, fl, ft = _fonts()
        now = datetime.now()
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%a %d %b %Y")

        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill="black")
                draw.rectangle((0, 0, WIDTH - 1, 14), fill="white")
                draw.text((4, 1), "Smart Chess Board", font=fs, fill="black")
                # Large clock
                draw.text((8, 18), time_str, font=fl, fill="white")
                draw.text((10, 42), date_str, font=fs, fill="white")
                draw.text((10, 54), "Press any button to start", font=ft, fill="white")

    # ── Clock thread ──────────────────────────────────────────────────────────

    def _start_clock(self):
        if self._clock_running:
            return
        self._clock_running = True
        self._clock_thread = threading.Thread(target=self._clock_loop, daemon=True)
        self._clock_thread.start()

    def _stop_clock(self):
        self._clock_running = False

    def _clock_loop(self):
        while self._clock_running and self._current_screen == "idle":
            self._draw_clock()
            time.sleep(1)
