# =============================================================================
# config.py
# Author : Richard Pu
# Created: 2026-06-12
# Purpose: Central configuration dataclass for the Jetson Smart Chess Board.
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
    chess_led_pin: int        = 19    # BOARD 19 (SPI1 MOSI)
    chess_led_count: int      = 64
    chess_led_brightness: int = 76    # 0-255 — ~30% prevents micro-USB overcurrent
    chess_led_channel: int    = 0     # unused by adafruit_neopixel_spi
    panel_led_pin: int        = 33    # unused until panel hardware present
    panel_led_count: int      = 22
    panel_led_brightness: int = 0
    panel_led_channel: int    = 1
    led_freq_hz: int          = 800_000  # unused by adafruit_neopixel_spi
    led_dma: int              = 10       # unused by adafruit_neopixel_spi
    led_invert: bool          = False    # unused by adafruit_neopixel_spi

    # ── Button hardware ───────────────────────────────────────────────────────
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
    dev_pin:  str = "1234"    # PIN for the dev menu on the dashboard

    # ── Gameplay ──────────────────────────────────────────────────────────────
    hint_dismiss_s: float    = 4.0   # seconds hint stays lit before auto-dismiss
    undo_max_half_moves: int = 10    # max half-moves the undo stack holds
    startup_led_delay_s: float = 0.015  # fast sweep — not the original 1 s per LED
    web_splash_hold_s: float   = 5.0    # how long to show splash before hiding
    menu_sleep_timeout_s: float = 60.0  # Inactivity seconds before menu sleep mode kicks in
    hint_dismiss_s: float    = 4.0   # seconds hint stays lit before auto-dismiss

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