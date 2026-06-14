# =============================================================================
# config.py
# Author : Richard Pu
# Created: 2026-06-12
# Purpose: Central configuration dataclass for the Jetson Smart Chess Board.
#          All tunable values live here — no more scattered magic numbers.
#          Values can be overridden via environment variables (loaded from .env).
# =============================================================================

import os
from dataclasses import dataclass, field


@dataclass
class GameConfig:
    # ── Stockfish ─────────────────────────────────────────────────────────────
    stockfish_path: str      = field(default_factory=lambda: os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish"))
    difficulty_min: int      = 1
    difficulty_max: int      = 20
    difficulty_default: int  = 10
    movetime_min_ms: int     = 3_000
    movetime_max_ms: int     = 12_000
    movetime_default_ms: int = 5_000

    # ── LED hardware ──────────────────────────────────────────────────────────
    # WS2812b data on SPI1 MOSI — BOARD 19, BCM line_offset 135
    chess_led_pin: int        = 135   # BOARD 19 (SPI1 MOSI) → BCM 135
    chess_led_count: int      = 64
    chess_led_brightness: int = 76    # ~30 % of 255 — prevents micro-USB overcurrent
    chess_led_channel: int    = 0
    panel_led_pin: int        = 33    # unused until panel hardware present
    panel_led_count: int      = 22
    panel_led_brightness: int = 0
    panel_led_channel: int    = 1
    led_freq_hz: int          = 800_000
    led_dma: int              = 10
    led_invert: bool          = False

    # ── Button hardware ───────────────────────────────────────────────────────
    # Physical BOARD pin numbers. All pins confirmed to have internal pull-ups
    # (rest HIGH, press to GND = LOW). Pin 29 lacks pull-up so button 5
    # is remapped to pin 36 which does have one.
    #
    # BOARD  7  → Button 1 (A/col 1)   — pull-up confirmed
    # BOARD 11  → Button 2 (B/col 2)   — pull-up confirmed
    # BOARD 13  → Button 3 (C/col 3)   — pull-up confirmed
    # BOARD 15  → Button 4 (D/col 4)   — pull-up confirmed
    # BOARD 36  → Button 5 (E/col 5)   — pull-up confirmed (was 29, no pull-up)
    # BOARD 31  → Button 6 (F/col 6)   — pull-up confirmed
    # BOARD 32  → OK / confirm (btn 9) — pull-up confirmed
    # BOARD 33  → Button 7 (G/col 7)   — pull-up confirmed
    # BOARD 35  → Button 8 (H/col 8)   — pull-up confirmed
    # BOARD 16  → Hint button           — pull-up confirmed
    # ── Button hardware ───────────────────────────────────────────────────────
    # Pins remapped to avoid the 3 pins with no internal pull-up (7, 16, 29).
    # All pins below confirmed SOLID HIGH with nothing connected.
    # Buttons wire: GPIO pin -> button -> GND. Pressed = LOW, released = HIGH.
    #
    # BOARD 24  → Button 1 (A/col 1)  remapped from 7  (no pull-up)
    # BOARD 11  → Button 2 (B/col 2)
    # BOARD 13  → Button 3 (C/col 3)
    # BOARD 15  → Button 4 (D/col 4)
    # BOARD 36  → Button 5 (E/col 5)  remapped from 29 (no pull-up)
    # BOARD 31  → Button 6 (F/col 6)
    # BOARD 33  → Button 7 (G/col 7)
    # BOARD 35  → Button 8 (H/col 8)
    # BOARD 32  → OK / confirm (btn 9)
    # BOARD 26  → Hint button          remapped from 16 (no pull-up)
    # ── Button matrix ─────────────────────────────────────────────────────────
    # 4-column × 3-row matrix with 470Ω pull-up resistors on columns.
    # Columns: BOARD 7, 11, 13, 15  (rest HIGH via resistors to 3.3V)
    # Rows:    BOARD 36, 37, 32     (driven LOW to scan)
    #
    # Layout:
    #          Col_1(7)  Col_2(11)  Col_3(13)  Col_4(15)
    #  Row_1(36)  btn1     btn2       btn3       btn4
    #  Row_2(37)  btn5     btn6       btn7       btn8
    #  Row_3(32)  btn9(OK)  hint
    #  Row_4(18)  btn2      btn1
    #  Row_5(22)  btn6      btn5
    #
    # Pin definitions live in hardware/buttons.py (COL_PINS, ROW_PINS).
    # These config values are kept for reference / tests only.
    button_debounce_s: float  = 0.30
    hint_pin: int             = 11    # Col_2 pin (hint cell is Row_3 Col_2)
    button_pins: dict         = field(default_factory=lambda: {
        1: 11,    # Row_4 Col_2
        2: 7,     # Row_4 Col_1
        3: 11,    # Row_1 Col_2
        4: 7,     # Row_1 Col_1
        5: 11,    # Row_5 Col_2
        6: 7,     # Row_5 Col_1
        7: 11,    # Row_2 Col_2
        8: 7,     # Row_2 Col_1
        9: 7,     # Row_3 Col_1  (OK / confirm)
    })

    # ── OLED ──────────────────────────────────────────────────────────────────
    # DFRobot OLED on I2C Bus 7 — BOARD pin 3 (SDA) and pin 5 (SCL)
    oled_i2c_port: int = field(default_factory=lambda: int(os.environ.get("OLED_I2C_PORT", "7")))
    oled_i2c_addr: int = field(default_factory=lambda: int(os.environ.get("OLED_I2C_ADDR", "0x3C"), 16))
    oled_width: int    = 128
    oled_height: int   = 64

    # ── Web server ────────────────────────────────────────────────────────────
    web_host: str = "0.0.0.0"
    web_port: int = 5000
    dev_pin:  str = "1337"    # PIN for the dev menu on the dashboard

    # ── Gameplay ──────────────────────────────────────────────────────────────
    hint_dismiss_s: float    = 4.0   # seconds hint stays lit before auto-dismiss
    undo_max_half_moves: int = 10    # max half-moves the undo stack holds
    startup_led_delay_s: float = 0.015  # fast sweep — not the original 1 s per LED

    # ── Game save (USB) ───────────────────────────────────────────────────────
    usb_mount_paths: list = field(default_factory=lambda: [
        "/media", "/mnt", "/run/media"
    ])
    pgn_subdir: str = "chess_games"   # created inside the USB root

    # ── Voice announcements ───────────────────────────────────────────────────
    tts_rate: int    = 165   # words per minute for espeak-ng / pyttsx3
    tts_volume: float = 0.9  # 0.0 – 1.0


# Singleton used across the whole project
CFG = GameConfig()