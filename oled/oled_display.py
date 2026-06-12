# =============================================================================
# oled/oled_display.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: SSD1306 128×64 I2C OLED driver for the Grove connector on the
#          Jetson. Renders all game screens with a clear, navigable layout.
#          Set MOCK_OLED=1 for headless use.
# =============================================================================

import os
import time
import logging
import threading
from datetime import datetime
from typing import Optional

from config import CFG

log = logging.getLogger(__name__)

MOCK = os.environ.get("MOCK_OLED", "0") == "1"

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
    class _FakeDevice:
        width  = CFG.oled_width
        height = CFG.oled_height
        def display(self, img): pass
        def cleanup(self): pass
        def hide(self): pass
        def show(self): pass

    class _FakeCanvas:
        def __init__(self, device):
            self._dev = device
        def __enter__(self):
            return _FakeDraw()
        def __exit__(self, *a):
            pass

    class _FakeDraw:
        def rectangle(self, *a, **k): pass
        def text(self, *a, **k):      pass
        def line(self, *a, **k):      pass
        def ellipse(self, *a, **k):   pass

    def _make_device():
        log.debug("[MOCK OLED] device created")
        return _FakeDevice()

    canvas = _FakeCanvas  # noqa: F811
    Image  = None

    def _load_font(size=10):
        return None

else:
    def _make_device():
        serial = i2c(port=CFG.oled_i2c_port, address=CFG.oled_i2c_addr)
        return ssd1306(serial, width=CFG.oled_width, height=CFG.oled_height)

    def _load_font(size=10):
        try:
            return ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size
            )
        except Exception:
            return ImageFont.load_default()


W, H = CFG.oled_width, CFG.oled_height

_FONT_SMALL = _FONT_MED = _FONT_LARGE = _FONT_TINY = None


def _fonts():
    global _FONT_SMALL, _FONT_MED, _FONT_LARGE, _FONT_TINY
    if _FONT_SMALL is None:
        _FONT_SMALL = _load_font(10)
        _FONT_MED   = _load_font(14)
        _FONT_LARGE = _load_font(20)
        _FONT_TINY  = _load_font(8)
    return _FONT_SMALL, _FONT_MED, _FONT_LARGE, _FONT_TINY


class OLEDDisplay:
    """
    Rich OLED UI.

    Screens
    -------
    idle              : clock + branding
    mode_select       : game-mode prompt  (1=AI  2=Online  3=Local)
    setup             : difficulty / timeout / colour setup
    game              : move history, whose turn, eval bar, status line
    hint              : suggested best move
    promotion_select  : choose promotion piece (Q R B N)
    undo_confirm      : confirm undo with move count
    checkmate         : winner announcement + move count
    new_game          : splash screen
    usb_saved         : brief USB save confirmation
    analysis          : post-game top-3 blunders
    loading           : startup progress bar
    """

    def __init__(self):
        self._device = _make_device()
        self._lock   = threading.Lock()
        self._current_screen = "idle"

        self._move_history: list[str] = []
        self._whose_turn   = "White"
        self._last_move    = ""
        self._status_msg   = ""
        self._game_mode    = ""
        self._difficulty   = 0
        self._hint_move    = ""
        self._eval_score   = 0

        self._clock_thread: Optional[threading.Thread] = None
        self._clock_running = False

        log.info(f"OLEDDisplay ready (mock={MOCK}, addr=0x{CFG.oled_i2c_addr:02X})")

    # ── Public API ────────────────────────────────────────────────────────────

    def show_idle(self):
        self._current_screen = "idle"
        self._start_clock()

    def show_mode_select(self):
        self._current_screen = "mode_select"
        self._stop_clock()
        self._draw_mode_select()

    def show_setup_difficulty(self, current: int = 0):
        self._current_screen = "setup"
        self._stop_clock()
        self._draw_setup("Difficulty", "Btn 1-8 to set", current, 8)

    def show_setup_timeout(self, current_ms: int = 0):
        self._current_screen = "setup"
        secs = current_ms // 1000
        self._draw_setup("Move Time", "Btn 1-8 to set", secs, 12)

    def show_setup_colour(self):
        self._current_screen = "setup"
        self._draw_colour_select()

    def show_game(
        self,
        whose_turn: str,
        last_move: str = "",
        move_history: list = None,
        status: str = "",
        eval_score: int = 0,
    ):
        self._current_screen = "game"
        self._stop_clock()
        self._whose_turn  = whose_turn
        self._last_move   = last_move
        self._status_msg  = status
        self._eval_score  = eval_score
        if move_history is not None:
            self._move_history = move_history
        self._draw_game()

    def push_move(self, uci: str, whose_turn_after: str, eval_score: int = 0):
        self._move_history.append(uci)
        self._last_move   = uci
        self._whose_turn  = whose_turn_after
        self._eval_score  = eval_score
        if self._current_screen == "game":
            self._draw_game()

    def show_hint(self, hint_uci: str):
        self._hint_move = hint_uci
        self._draw_hint(hint_uci)

    def show_status(self, msg: str):
        self._status_msg = msg
        if self._current_screen == "game":
            self._draw_game()

    def show_checkmate(self, winner: str):
        self._current_screen = "checkmate"
        self._stop_clock()
        self._draw_checkmate(winner)

    def show_new_game(self):
        self._current_screen = "new_game"
        self._draw_new_game()

    def show_loading(self, progress: int, total: int = 64):
        self._draw_loading(progress, total)

    def show_promotion_select(self, colour: str = "White"):
        """
        Prompt the player to choose a promotion piece.
        Shows: Q  R  B  N  with their button numbers (1-4) clearly labelled.
        """
        self._current_screen = "promotion"
        self._draw_promotion_select(colour)

    def show_undo_confirm(self, half_moves: int):
        """Show a brief confirmation that N half-moves were undone."""
        self._current_screen = "undo"
        self._draw_undo_confirm(half_moves)

    def show_usb_saved(self, filename: str):
        """Show a brief confirmation that the game was saved to USB."""
        self._current_screen = "usb_saved"
        self._draw_usb_saved(filename)

    def show_analysis(self, blunders: list):
        """
        Display the top blunders after the game.
        blunders: list of dicts: [{'move': 'e4d5', 'loss': 320, 'best': 'g1f3'}, ...]
        """
        self._current_screen = "analysis"
        self._draw_analysis(blunders)

    def show_local_game_start(self):
        """Splash for local two-player mode."""
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Local 2 Player", font=fm, fill="black")
                draw.text((4, 20), "White plays first.", font=fs, fill="white")
                draw.text((4, 33), "Pass the board each", font=ft, fill="white")
                draw.text((4, 43), "turn. Good luck!", font=ft, fill="white")

    def clear(self):
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")

    def cleanup(self):
        self._stop_clock()
        self.clear()
        self._device.cleanup()

    # ── Drawing routines ──────────────────────────────────────────────────────

    def _draw_mode_select(self):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Select Game Mode", font=fs, fill="black")
                draw.text((4, 17), "1: vs AI (Stockfish)", font=fs, fill="white")
                draw.text((4, 28), "2: Online (Lichess)",  font=fs, fill="white")
                draw.text((4, 39), "3: Local 2 Player",    font=fs, fill="white")
                draw.line((0, 51, W - 1, 51), fill="white")
                draw.text((4, 54), "NVIDIA Jetson Orin Nano", font=ft, fill="white")

    def _draw_setup(self, label: str, prompt: str, current: int, max_val: int):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), label, font=fm, fill="black")
                draw.text((4, 18), prompt, font=fs, fill="white")
                # Progress bar
                draw.rectangle((4, 34, W - 4, 44), outline="white")
                if current > 0:
                    bar_w = int((current / max_val) * (W - 8))
                    draw.rectangle((4, 34, 4 + bar_w, 44), fill="white")
                    draw.text((4, 48), f"Current: {current}", font=fs, fill="white")
                else:
                    draw.text((4, 48), "Waiting for input...", font=ft, fill="white")

    def _draw_colour_select(self):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Choose Your Colour", font=fm, fill="black")
                draw.rectangle((4, 20, 58, 50), fill="white")
                draw.text((9, 27), "WHITE", font=fs, fill="black")
                draw.text((9, 39), "Btn 1",  font=ft, fill="black")
                draw.rectangle((66, 20, 122, 50), outline="white")
                draw.text((71, 27), "BLACK", font=fs, fill="white")
                draw.text((71, 39), "Btn 2",  font=ft, fill="white")

    def _draw_game(self):
        fs, fm, fl, ft = _fonts()
        history = self._move_history

        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")

                # ── Top bar: whose turn ──────────────────────────────────────
                bar_color = "white" if self._whose_turn == "White" else "gray"
                draw.rectangle((0, 0, W - 1, 13), fill=bar_color)
                tc = "black" if self._whose_turn == "White" else "white"
                draw.text((4, 1), f"{self._whose_turn}'s Turn", font=fs, fill=tc)
                mode_lbl = {"Stockfish": "AI", "OnlineHuman": "Net",
                            "LocalHuman": "2P"}.get(self._game_mode, "")
                draw.text((96, 1), mode_lbl, font=ft, fill=tc)

                # ── Eval bar (small, below top bar) ─────────────────────────
                score   = max(-500, min(500, self._eval_score))
                pct     = (score + 500) / 1000.0  # 0.0–1.0, white advantage right
                bar_len = int(pct * (W - 2))
                draw.rectangle((0, 14, bar_len, 16), fill="white")
                draw.rectangle((bar_len, 14, W - 1, 16), fill="gray")

                # ── Move history (last 3 pairs) ──────────────────────────────
                y = 19
                draw.text((0, y), "Moves:", font=ft, fill="white")
                y += 9
                pairs = []
                for i in range(0, len(history), 2):
                    w = history[i]
                    b = history[i + 1] if i + 1 < len(history) else "..."
                    pairs.append((i // 2 + 1, w, b))
                for num, wm, bm in pairs[-3:]:
                    draw.text((0,  y), f"{num}.", font=ft, fill="white")
                    draw.text((16, y), wm,         font=ft, fill="white")
                    draw.text((64, y), bm,          font=ft, fill="white")
                    y += 9

                # ── Status bar ────────────────────────────────────────────────
                draw.line((0, H - 12, W - 1, H - 12), fill="white")
                status = (self._status_msg or
                          (f"Last: {self._last_move}" if self._last_move
                           else "Game in progress"))
                # Truncate so it fits
                draw.text((2, H - 10), status[:22], font=ft, fill="white")

    def _draw_hint(self, uci: str):
        fs, fm, fl, ft = _fonts()
        readable = (f"{uci[:2].upper()} \u2192 {uci[2:4].upper()}"
                    if len(uci) >= 4 else uci)
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Hint", font=fm, fill="black")
                draw.text((14, 22), readable, font=fl, fill="white")
                draw.text((4, 50), "Btn HINT again to dismiss", font=ft, fill="white")

    def _draw_promotion_select(self, colour: str):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Promote Pawn!", font=fm, fill="black")
                draw.text((4, 16), colour, font=ft, fill="white")
                # Four options in a 2×2 grid
                draw.rectangle((4,  26, 60, 42), outline="white")
                draw.text((8,  29), "1: Queen",  font=ft, fill="white")
                draw.rectangle((66, 26, 122, 42), outline="white")
                draw.text((70, 29), "2: Rook",   font=ft, fill="white")
                draw.rectangle((4,  46, 60, 62), outline="white")
                draw.text((8,  49), "3: Bishop", font=ft, fill="white")
                draw.rectangle((66, 46, 122, 62), outline="white")
                draw.text((70, 49), "4: Knight", font=ft, fill="white")

    def _draw_undo_confirm(self, half_moves: int):
        fs, fm, fl, ft = _fonts()
        noun = "move" if half_moves <= 2 else f"{half_moves // 2} moves"
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Undo", font=fm, fill="black")
                draw.text((4, 18), f"Rewound {noun}.", font=fs, fill="white")
                draw.text((4, 32), "Board restored.", font=fs, fill="white")
                draw.text((4, 50), "Your turn.", font=fs, fill="white")

    def _draw_usb_saved(self, filename: str):
        fs, fm, fl, ft = _fonts()
        short = filename[:18] if len(filename) > 18 else filename
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Saved to USB!", font=fm, fill="black")
                draw.text((4, 18), "File:", font=ft, fill="white")
                draw.text((4, 28), short, font=ft, fill="white")
                draw.text((4, 46), "Safely eject USB", font=ft, fill="white")
                draw.text((4, 55), "when done.", font=ft, fill="white")

    def _draw_analysis(self, blunders: list):
        """Display post-game top blunders (up to 3)."""
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Game Analysis", font=fm, fill="black")
                if not blunders:
                    draw.text((4, 20), "No major blunders!", font=fs, fill="white")
                    draw.text((4, 34), "Great game.", font=fs, fill="white")
                else:
                    y = 17
                    draw.text((4, y), "Top mistakes:", font=ft, fill="white")
                    y += 10
                    for i, b in enumerate(blunders[:3]):
                        move = b.get("move", "?")
                        best = b.get("best", "?")
                        loss = b.get("loss", 0)
                        draw.text((4, y),
                                  f"{i+1}. {move} (-{loss//100:.1f}) best:{best}",
                                  font=ft, fill="white")
                        y += 9

    def _draw_checkmate(self, winner: str):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((2, 2, W - 3, H - 3), outline="white")
                draw.rectangle((5, 5, W - 6, H - 6), outline="white")
                draw.text((8, 10), "CHECKMATE!", font=fm, fill="white")
                draw.text((8, 28), f"{winner} wins!", font=fs, fill="white")
                draw.text((8, 40), f"{len(self._move_history)} moves", font=ft, fill="white")
                draw.text((8, 52), "OK = new game", font=ft, fill="white")

    def _draw_new_game(self):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.text((20, 18), "NEW GAME!", font=fm, fill="white")
                draw.text((30, 38), "Good luck!", font=fs, fill="white")

    def _draw_loading(self, progress: int, total: int = 64):
        fs, fm, fl, ft = _fonts()
        pct   = int((progress / total) * 100)
        bar_w = int((progress / total) * (W - 8))
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.text((4,  6), "Smart Chess Board",  font=fs, fill="white")
                draw.text((4, 18), "NVIDIA Jetson Orin", font=ft, fill="white")
                draw.rectangle((4, 34, W - 4, 46), outline="white")
                if bar_w > 0:
                    draw.rectangle((4, 34, 4 + bar_w, 46), fill="white")
                draw.text((4, 50), f"Starting... {pct}%", font=ft, fill="white")

    def _draw_clock(self):
        fs, fm, fl, ft = _fonts()
        now      = datetime.now()
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%a %d %b %Y")
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Smart Chess Board", font=fs, fill="black")
                draw.text((8, 18), time_str, font=fl, fill="white")
                draw.text((10, 42), date_str, font=fs, fill="white")
                draw.text((10, 54), "Press any btn to start", font=ft, fill="white")

    # ── Clock thread ──────────────────────────────────────────────────────────

    def _start_clock(self):
        if self._clock_running:
            return
        self._clock_running = True
        self._clock_thread  = threading.Thread(target=self._clock_loop,
                                                daemon=True)
        self._clock_thread.start()

    def _stop_clock(self):
        self._clock_running = False

    def _clock_loop(self):
        while self._clock_running and self._current_screen == "idle":
            self._draw_clock()
            time.sleep(1)
