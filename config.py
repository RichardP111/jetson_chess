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
    # BCM key numbers as used by Jetson.GPIO in BCM mode.
    # Confirmed by matching BOARD ChannelInfo.gpio_name → BCM dict keys.
    #
    # BOARD  7  PAC.06  → BCM  4
    # BOARD 11  PR.04   → BCM 17
    # BOARD 13  PY.00   → BCM 27
    # BOARD 15  PN.01   → BCM 22
    # BOARD 16  PY.04   → BCM 23  (hint)
    # BOARD 29  PQ.05   → BCM  5
    # BOARD 31  PQ.06   → BCM  6
    # BOARD 32  PG.06   → BCM 12  (OK / confirm)
    # BOARD 33  PH.00   → BCM 13
    # BOARD 35  PI.02   → BCM 19
    button_debounce_s: float  = 0.30
    hint_pin: int             = 23    # BOARD 16 → BCM 23
    button_pins: dict         = field(default_factory=lambda: {
        1:  4,    # BOARD  7  — A / column 1
        2: 17,    # BOARD 11  — B / column 2
        3: 27,    # BOARD 13  — C / column 3
        4: 22,    # BOARD 15  — D / column 4
        5:  5,    # BOARD 29  — E / column 5
        6:  6,    # BOARD 31  — F / column 6
        7: 13,    # BOARD 33  — G / column 7
        8: 19,    # BOARD 35  — H / column 8
        9: 12,    # BOARD 32  — OK / confirm
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