from __future__ import annotations
# =============================================================================
# oled/oled_display.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-15
# Purpose: SSD1306 128×64 I2C OLED driver for the Grove connector on the
#          Jetson. Features a non-blocking threaded overlay timeout engine
#          to automatically clear transient alerts after a readable 3s window.
# =============================================================================

import os
import time
import logging
import threading
from datetime import datetime
from typing import Optional, Callable, Any

from config import CFG
import web.server as web_server

log = logging.getLogger(__name__)

MOCK = os.environ.get("MOCK_OLED", "0") == "1"

_make_device_fn: Callable[[], Any]
_load_font_fn: Callable[[int], Any]

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

    def _make_device_mock() -> Any:
        log.debug("[MOCK OLED] device created")
        return _FakeDevice()

    def _load_font_mock(size: int = 10) -> Any:
        return None

    canvas = _FakeCanvas  # noqa: F811
    Image  = None
    _make_device_fn = _make_device_mock
    _load_font_fn   = _load_font_mock

else:
    def _make_device_real() -> Any:
        serial = i2c(port=CFG.oled_i2c_port, address=CFG.oled_i2c_addr)
        return ssd1306(serial, width=CFG.oled_width, height=CFG.oled_height)

    def _load_font_real(size: int = 10) -> Any:
        try:
            return ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size
            )
        except Exception:
            return ImageFont.load_default()

    _make_device_fn = _make_device_real
    _load_font_fn   = _load_font_real


W, H = CFG.oled_width, CFG.oled_height

_FONT_SMALL = _FONT_MED = _FONT_LARGE = _FONT_TINY = None


def _fonts():
    global _FONT_SMALL, _FONT_MED, _FONT_LARGE, _FONT_TINY
    if _FONT_SMALL is None:
        _FONT_SMALL = _load_font_fn(10)
        _FONT_MED   = _load_font_fn(14)
        _FONT_LARGE = _load_font_fn(20)
        _FONT_TINY  = _load_font_fn(8)
    return _FONT_SMALL, _FONT_MED, _FONT_LARGE, _FONT_TINY


class OLEDDisplay:
    def __init__(self):
        self._device = _make_device_fn()
        self._lock   = threading.Lock()
        self._current_screen = "idle"
        
        # ── OVERLAY MANAGEMENT TRACKING CHIPS ──
        self._persistent_screen = "idle"
        self._overlay_timer: Optional[threading.Timer] = None
        self._setup_label = ""
        self._setup_prompt = ""
        self._setup_current = 0
        self._setup_max = 8
        self._setup_current_ms = 0

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

    # ── Overlay Reversion Framework ───────────────────────────────────────────

    def _start_overlay_timer(self, delay: float = 3.0):
        """Saves current display assets and fires an automated clear window."""
        if self._overlay_timer:
            self._overlay_timer.cancel()
        self._overlay_timer = threading.Timer(delay, self._revert_to_persistent)
        self._overlay_timer.start()

    def _revert_to_persistent(self):
        """Thread worker invocation callback to restore operational contexts."""
        self._overlay_timer = None
        
        if self._persistent_screen == "idle":
            self._current_screen = "idle"
            self._start_clock()
        elif self._persistent_screen == "menu_sleep":
            self._current_screen = "menu_sleep"
            self._start_clock()
        elif self._persistent_screen == "mode_select":
            self._current_screen = "mode_select"
            self._draw_mode_select()
        elif self._persistent_screen == "setup_difficulty":
            self._current_screen = "setup"
            self._draw_setup(self._setup_label, self._setup_prompt, self._setup_current, self._setup_max)
        elif self._persistent_screen == "setup_timeout":
            self._current_screen = "setup"
            self._draw_time_select(self._setup_current_ms)
        elif self._persistent_screen == "setup_colour":
            self._current_screen = "setup"
            self._draw_colour_select()
        elif self._persistent_screen == "game":
            self._current_screen = "game"
            self._draw_game()

    # ── Public API ────────────────────────────────────────────────────────────

    def show_idle(self):
        if self._overlay_timer: self._overlay_timer.cancel()
        self._persistent_screen = "idle"
        self._current_screen = "idle"
        self._start_clock()

    def show_menu_sleep(self):
        if self._overlay_timer: self._overlay_timer.cancel()
        self._persistent_screen = "menu_sleep"
        self._current_screen = "menu_sleep"
        self._start_clock()

    def show_mode_select(self):
        if self._overlay_timer: self._overlay_timer.cancel()
        self._persistent_screen = "mode_select"
        self._current_screen = "mode_select"
        self._stop_clock()
        self._draw_mode_select()

    def show_setup_difficulty(self, current: int = 0):
        if self._overlay_timer: self._overlay_timer.cancel()
        self._persistent_screen = "setup_difficulty"
        self._setup_label = "Difficulty"
        self._setup_prompt = "1-8 or web slider"
        self._setup_current = current
        self._setup_max = 8
        self._current_screen = "setup"
        self._stop_clock()
        self._draw_setup("Difficulty", "1-8 or web slider", current, 8)

    def show_setup_timeout(self, current_ms: int = 0):
        if self._overlay_timer: self._overlay_timer.cancel()
        self._persistent_screen = "setup_timeout"
        self._setup_current_ms = current_ms
        self._current_screen = "setup"
        self._stop_clock()
        self._draw_time_select(current_ms)

    def show_setup_colour(self):
        if self._overlay_timer: self._overlay_timer.cancel()
        self._persistent_screen = "setup_colour"
        self._current_screen = "setup"
        self._draw_colour_select()

    def show_game(
        self,
        whose_turn: str,
        last_move: str = "",
        move_history: list = None,  # type: ignore[assignment]
        status: str = "",
        eval_score: int = 0,
    ):
        if self._overlay_timer: self._overlay_timer.cancel()
        self._persistent_screen = "game"
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
        else:
            # Displays notification panel and schedules automated clear timer after 3 seconds
            self._draw_status_overlay(msg)
            self._start_overlay_timer(3.0)

    def show_checkmate(self, winner: str):
        if self._overlay_timer: self._overlay_timer.cancel()
        self._current_screen = "checkmate"
        self._stop_clock()
        self._draw_checkmate(winner)

    def show_new_game(self):
        self._current_screen = "new_game"
        self._draw_new_game()

    def show_loading(self, progress: int, total: int = 64):
        self._current_screen = "loading"
        self._draw_loading(progress, total)

    def show_promotion_select(self, colour: str = "White"):
        self._current_screen = "promotion"
        self._draw_promotion_select(colour)

    def show_undo_confirm(self, half_moves: int):
        self._current_screen = "undo"
        self._draw_undo_confirm(half_moves)
        self._start_overlay_timer(3.0)  # Reverts to persistent background layout smoothly after 3s

    def show_usb_saved(self, filename: str):
        self._current_screen = "usb_saved"
        self._draw_usb_saved(filename)
        self._start_overlay_timer(3.0)  # Reverts to persistent background layout smoothly after 3s

    def show_analysis(self, blunders: list):
        self._current_screen = "analysis"
        self._draw_analysis(blunders)

    def show_local_game_start(self):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Local 2 Player", font=fm, fill="black")
                draw.text((4, 20), "White plays first.", font=fs, fill="white")
                draw.text((4, 33), "Pass the board each", font=ft, fill="white")
                draw.text((4, 43), "turn. Good luck!", font=ft, fill="white")
        
        web_server.update_oled_text([
            "Local 2 Player",
            "White plays first.",
            "Pass the board each",
            "turn. Good luck!"
        ])

    def show_draw(self, reason: str):
        if self._overlay_timer: self._overlay_timer.cancel()
        self._current_screen = "draw"
        self._stop_clock()
        self._draw_draw(reason)

    def show_pass_board(self, next_turn: str, move_count: int):
        self._current_screen = "pass"
        self._draw_pass_board(next_turn, move_count)

    def show_network_info(self, ip: str):
        self._current_screen = "network"
        self._draw_network_info(ip)

    def show_coordinate_prompt(self, step: str, partial: str = ""):
        """Public endpoint for tracking matrix coordinates."""
        self._draw_coordinate_prompt(step, partial)

    def _draw_coordinate_prompt(self, step: str, partial: str = ""):
        """
        Protected endpoint alias. Maps directly to the drawing routine 
        to satisfy legacy or direct calls from main.py without linter complaints.
        """
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                label = "Select FROM" if not partial else f"From: {partial}"
                draw.text((4, 1), label, font=fs, fill="black")
                if step == "col":
                    draw.text((4, 18), "Press 1-8 for", font=ft, fill="white")
                    draw.text((4, 28), "A  B  C  D", font=fs, fill="white")
                    draw.text((4, 40), "E  F  G  H", font=fs, fill="white")
                elif step == "row":
                    draw.text((4, 18), "Press 1-8 for", font=ft, fill="white")
                    draw.text((4, 28), "row  1-8", font=fm, fill="white")
                    if partial:
                        draw.text((4, 48), f"Col: {partial[-1].upper()}", font=fs, fill="white")

        # Push updates seamlessly to the glowing cyan web dashboard mirror widget
        web_server.update_oled_text([
            label,
            "Matrix input scan active",
            f"Selected segment: {partial.upper()}" if partial else "Awaiting row/col input...",
            "Step: Choose column" if step == "col" else "Step: Choose row"
        ])

    def cleanup(self):
        if self._overlay_timer:
            self._overlay_timer.cancel()
        self._stop_clock()
        self.clear()
        self._device.cleanup()

    def show_move_input_screen(self, current_turn: str, slots: str, prompt: str):
        """Displays character-by-character coordinate progress using a crisp layout."""
        fs, fm, fl, ft = _fonts()
        c1 = slots[0].upper() if len(slots) > 0 else "_"
        r1 = slots[1]         if len(slots) > 1 else "_"
        c2 = slots[2].upper() if len(slots) > 2 else "_"
        r2 = slots[3]         if len(slots) > 3 else "_"
        move_str = f"{c1}{r1} \u2192 {c2}{r2}"
        
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 15), fill="white")
                draw.text((4, 1), f"{current_turn}'s Move", font=fs, fill="black")
                draw.text((22, 22), move_str, font=fl, fill="white")
                draw.line((0, H - 12, W - 1, H - 12), fill="white")
                draw.text((4, H - 10), prompt[:24], font=ft, fill="white")

        web_server.update_oled_text([
            f"{current_turn}'s Move",
            f"   {move_str}",
            "",
            prompt
        ])

    def clear(self):
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")

    # ── Drawing routines ──────────────────────────────────────────────────────

    def _draw_mode_select(self):
        fs, fm, fl, ft = _fonts()
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "localhost"
        dashboard_url = f"http://{ip}:{CFG.web_port}"

        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 13), fill="white")
                draw.text((4, 1), "Select Game Mode", font=fs, fill="black")
                draw.text((4, 16), "1: vs AI (Stockfish)", font=ft, fill="white")
                draw.text((4, 25), "2: Online (Lichess)",  font=ft, fill="white")
                draw.text((4, 34), "3: Local 2 Player",    font=ft, fill="white")
                draw.text((4, 43), "or select on dashboard", font=ft, fill="white")
                draw.line((0, 52, W - 1, 52), fill="white")
                draw.text((4, 54), dashboard_url, font=ft, fill="white")

        web_server.update_oled_text([
            "Select Game Mode",
            "1: vs AI  |  2: Lichess",
            "3: Local 2 Player",
            f"Dashboard: {ip}:{CFG.web_port}"
        ])

    def _draw_setup(self, label: str, prompt: str, current: int, max_val: int):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), label, font=fm, fill="black")
                draw.text((4, 18), prompt, font=fs, fill="white")
                draw.rectangle((4, 34, W - 4, 44), outline="white")
                if current > 0:
                    bar_w = int((current / max_val) * (W - 8))
                    draw.rectangle((4, 34, 4 + bar_w, 44), fill="white")
                    draw.text((4, 48), f"Current: {current}", font=fs, fill="white")
                else:
                    draw.text((4, 48), "Waiting for input...", font=ft, fill="white")

        web_server.update_oled_text([
            f"Setup: {label}",
            prompt,
            f"Current Target: {current}" if current > 0 else "Awaiting configuration...",
            ""
        ])

    def _draw_time_select(self, current_ms: int = 0):
        fs, fm, fl, ft = _fonts()
        TIME_OPTIONS = [1, 2, 3, 5, 8, 12, 20, 30]
        current_s = current_ms // 1000
        HDR, GAP, ROWS, COLS = 13, 2, 2, 4
        cell_w = W // COLS                       
        cell_h = (H - HDR - GAP) // ROWS        
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, HDR - 1), fill="white")
                draw.text((4, 2), "Move Time", font=fs, fill="black")
                for i, secs in enumerate(TIME_OPTIONS):
                    c, r = i % COLS, i // COLS
                    x1 = c * cell_w + 1
                    y1 = HDR + GAP + r * cell_h + 1
                    x2 = x1 + cell_w - 3
                    y2 = y1 + cell_h - 3
                    selected = (secs == current_s)
                    if selected:
                        draw.rectangle((x1, y1, x2, y2), fill="white")
                        tc = "black"
                    else:
                        draw.rectangle((x1, y1, x2, y2), outline="white")
                        tc = "white"
                    label = f"{secs}s"
                    draw.text((x1 + 3, y1 + 4), label, font=ft, fill=tc)

        web_server.update_oled_text([
            "Move Time Selection",
            "Options: 1s 2s 3s 5s",
            "         8s 12s 20s 30s",
            f"Active Choice: {current_s}s" if current_s > 0 else "Awaiting Selection..."
        ])

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

        web_server.update_oled_text([
            "Choose Your Colour",
            "Button 1 -> WHITE",
            "Button 2 -> BLACK",
            "Awaiting input..."
        ])

    @staticmethod
    def _uci_to_san_approx(uci: str) -> str:
        if len(uci) < 4:
            return uci
        promo = uci[4].upper() if len(uci) >= 5 else ""
        return uci[2:4] + promo if not promo else uci[2:4] + "=" + promo

    def _draw_game(self):
        fs, fm, fl, ft = _fonts()
        history = self._move_history

        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                bar_color = "white" if self._whose_turn == "White" else "gray"
                draw.rectangle((0, 0, W - 1, 13), fill=bar_color)
                tc = "black" if self._whose_turn == "White" else "white"
                draw.text((4, 1), f"{self._whose_turn}'s Turn", font=fs, fill=tc)
                mode_lbl = {"Stockfish": "AI", "OnlineHuman": "Net",
                            "LocalHuman": "2P"}.get(self._game_mode, "")
                draw.text((96, 1), mode_lbl, font=ft, fill=tc)

                score   = max(-500, min(500, self._eval_score))
                pct     = (score + 500) / 1000.0  
                bar_len = int(pct * (W - 2))
                draw.rectangle((0, 14, bar_len, 16), fill="white")
                draw.rectangle((bar_len, 14, W - 1, 16), fill="gray")

                y = 19
                draw.text((0, y), "Moves:", font=ft, fill="white")
                y += 9
                pairs = []
                for i in range(0, len(history), 2):
                    w = history[i]
                    b = history[i + 1] if i + 1 < len(history) else "..."
                    pairs.append((i // 2 + 1, w, b))
                for num, wm, bm in pairs[-3:]:
                    wm_r = self._uci_to_san_approx(wm)
                    bm_r = self._uci_to_san_approx(bm) if bm != "..." else "..."
                    draw.text((0,  y), f"{num}.", font=ft, fill="white")
                    draw.text((16, y), wm_r,       font=ft, fill="white")
                    draw.text((64, y), bm_r,        font=ft, fill="white")
                    y += 9

                draw.line((0, H - 12, W - 1, H - 12), fill="white")
                status = (self._status_msg or
                          (f"Last: {self._last_move}" if self._last_move
                           else "Game in progress"))
                draw.text((2, H - 10), status[:24], font=ft, fill="white")

        last_move_summary = f"Last Move: {pairs[-1][0]}. {pairs[-1][1].upper()} {pairs[-1][2].upper()}" if pairs else "No moves recorded yet"
        web_server.update_oled_text([
            f"{self._whose_turn}'s Turn ({mode_lbl})",
            f"Eval Advantage: {self._eval_score}",
            last_move_summary,
            status
        ])

    def _draw_hint(self, uci: str):
        fs, fm, fl, ft = _fonts()
        readable = f"{uci[:2].upper()} \u2192 {uci[2:4].upper()}" if len(uci) >= 4 else uci
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Hint", font=fm, fill="black")
                draw.text((14, 22), readable, font=fl, fill="white")
                draw.text((4, 50), "Press hint to dismiss", font=ft, fill="white")

        web_server.update_oled_text([
            "Engine Hint",
            readable,
            "Press HINT on board",
            "to dismiss overlay"
        ])

    def _draw_promotion_select(self, colour: str):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Promote Pawn!", font=fm, fill="black")
                draw.text((4, 16), colour, font=ft, fill="white")
                draw.rectangle((4,  26, 60, 42), outline="white")
                draw.text((8,  29), "1: Queen",  font=ft, fill="white")
                draw.rectangle((66, 26, 122, 42), outline="white")
                draw.text((70, 29), "2: Rook",   font=ft, fill="white")
                draw.rectangle((4,  46, 60, 62), outline="white")
                draw.text((8,  49), "3: Bishop", font=ft, fill="white")
                draw.rectangle((66, 46, 122, 62), outline="white")
                draw.text((70, 49), "4: Knight", font=ft, fill="white")

        web_server.update_oled_text([
            f"Promote Pawn! ({colour})",
            "1: Queen   |  2: Rook",
            "3: Bishop  |  4: Knight",
            "Select mapping index..."
        ])

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

        web_server.update_oled_text([
            "Undo Confirmation",
            f"Rewound {noun}.",
            "Board state restored.",
            "Resuming input turn loop..."
        ])

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

        web_server.update_oled_text([
            "Saved to USB Drive!",
            f"File: {short}",
            "Data flush complete.",
            "Safe to extract thumb-drive."
        ])

    def _draw_analysis(self, blunders: list):
        fs, fm, fl, ft = _fonts()
        mirror_lines = ["Game Analysis"]
        
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Game Analysis", font=fm, fill="black")
                if not blunders:
                    draw.text((4, 20), "No major blunders!", font=fs, fill="white")
                    draw.text((4, 34), "Great game.", font=fs, fill="white")
                    mirror_lines.extend(["No major blunders!", "Great game.", ""])
                else:
                    y = 17
                    draw.text((4, y), "Top mistakes:", font=ft, fill="white")
                    mirror_lines.append("Top mistakes:")
                    y += 10
                    for i, b in enumerate(blunders[:3]):
                        move, best, loss = b.get("move", "?"), b.get("best", "?"), b.get("loss", 0)
                        draw.text((4, y), f"{i+1}. {move} (-{loss//100:.1f}) best:{best}", font=ft, fill="white")
                        if len(mirror_lines) < 4:
                            mirror_lines.append(f"{i+1}: {move.upper()} (-{loss/100:.1f}) best:{best.upper()}")
                        y += 9

        while len(mirror_lines) < 4: mirror_lines.append("")
        web_server.update_oled_text(mirror_lines[:4])

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

        web_server.update_oled_text([
            "CHECKMATE!",
            f"Winner: {winner}!",
            f"Game lasted {len(self._move_history)} plies",
            "Press OK (9) to clear board"
        ])

    def _draw_new_game(self):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.text((20, 18), "NEW GAME!", font=fm, fill="white")
                draw.text((30, 38), "Good luck!", font=fs, fill="white")

        web_server.update_oled_text([ "NEW GAME INITIALIZED", "", "Good luck, players!", "" ])

    def _draw_loading(self, progress: int, total: int = 64):
        fs, fm, fl, ft = _fonts()
        pct   = int((progress / total) * 100)
        bar_w = int((progress / total) * (W - 8))
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.text((4,  0), "Smart Chess Board",  font=fs, fill="white")
                draw.text((4, 11), "NVIDIA Jetson Orin", font=ft, fill="white")
                draw.rectangle((4, 21, W - 4, 26), outline="white")
                if bar_w > 0:
                    draw.rectangle((4, 21, 4 + bar_w, 26), fill="white")
                draw.text((4, 31), f"Starting... {pct}%", font=ft, fill="white")
                draw.text((4, 54), "Made with \u2665 by Richard", font=ft, fill="white")

        web_server.update_oled_text([
            "Smart Chess Board",
            "NVIDIA Jetson Orin",
            f"Starting... {pct}%",
            "Made with \u2665 by Richard"
        ])

    def _draw_status_overlay(self, msg: str):
        fs, fm, fl, ft = _fonts()
        words, lines, line = msg.split(), [], ""
        for w in words:
            test = (line + " " + w).strip()
            if len(test) <= 20: line = test
            else:
                if line: lines.append(line)
                line = w
        if line: lines.append(line)
            
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Status", font=fs, fill="black")
                y = 18
                for l in lines[:3]:
                    draw.text((4, y), l, font=fs, fill="white")
                    y += 14

        mirror_lines = ["System Status Notification"] + lines
        while len(mirror_lines) < 4: mirror_lines.append("")
        web_server.update_oled_text(mirror_lines[:4])

    def _draw_draw(self, reason: str):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((2, 2, W - 3, H - 3), outline="white")
                draw.rectangle((5, 5, W - 6, H - 6), outline="white")
                draw.text((8, 10), "DRAW!", font=fm, fill="white")
                draw.text((8, 28), reason[:20], font=fs, fill="white")
                draw.text((8, 40), f"{len(self._move_history)} moves", font=ft, fill="white")
                draw.text((8, 52), "OK = new game", font=ft, fill="white")

        web_server.update_oled_text([
            "MATCH DRAW!",
            f"Reason: {reason}",
            f"Total moves: {len(self._move_history)}",
            "Press OK (9) to clear board"
        ])

    def _draw_pass_board(self, next_turn: str, move_count: int):
        fs, fm, fl, ft = _fonts()
        colour_fill = "white" if next_turn == "White" else "gray"
        text_fill   = "black" if next_turn == "White" else "white"
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 18), fill=colour_fill)
                draw.text((4, 2), f"{next_turn}'s Turn", font=fm, fill=text_fill)
                draw.text((4, 22), "Pass the board", font=fs, fill="white")
                draw.text((4, 34), "to next player", font=fs, fill="white")
                draw.text((4, 50), f"Move {move_count // 2 + 1}", font=ft, fill="white")

        web_server.update_oled_text([
            f"PASS BOARD -> {next_turn.upper()}",
            "Hand device over to",
            "the opposing player.",
            f"Prepare for Move {move_count // 2 + 1}"
        ])

    def _draw_network_info(self, ip: str):
        fs, fm, fl, ft = _fonts()
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Web Dashboard", font=fs, fill="black")
                draw.text((4, 18), "Connect at:", font=ft, fill="white")
                draw.text((4, 28), f"http://{ip}", font=ft, fill="white")
                draw.text((4, 40), f"port 5000", font=ft, fill="white")
                draw.line((0, 52, W - 1, 52), fill="white")
                draw.text((4, 54), "Starting game...", font=ft, fill="white")

        web_server.update_oled_text([
            "Network Initialization",
            f"Host URL: http://{ip}",
            "Service Port: 5000",
            "Starting up engines..."
        ])

    def _draw_clock(self):
        fs, fm, fl, ft = _fonts()
        now      = datetime.now()
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%a %d %b %Y")
        bottom_text = "Press any btn to wake" if self._current_screen == "menu_sleep" else "Press any btn to start"
        
        with self._lock:
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, W - 1, H - 1), fill="black")
                draw.rectangle((0, 0, W - 1, 14), fill="white")
                draw.text((4, 1), "Smart Chess Board", font=fs, fill="black")
                draw.text((8, 18), time_str, font=fl, fill="white")
                draw.text((10, 42), date_str, font=fs, fill="white")
                draw.text((10, 54), bottom_text, font=ft, fill="white")

        web_server.update_oled_text([
            "Smart Chess Board",
            f"System Time: {time_str}",
            date_str,
            bottom_text
        ])

    # ── Clock thread ──────────────────────────────────────────────────────────

    def _start_clock(self):
        if self._clock_running: return
        self._clock_running = True
        self._clock_thread  = threading.Thread(target=self._clock_loop, daemon=True)
        self._clock_thread.start()

    def _stop_clock(self):
        self._clock_running = False

    def _clock_loop(self):
        while self._clock_running and self._current_screen in ("idle", "menu_sleep"):
            self._draw_clock()
            time.sleep(1)