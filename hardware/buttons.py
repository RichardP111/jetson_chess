"""
hardware/buttons.py
Button input via Jetson Orin Nano GPIO.

Maps the 10 original Arduino buttons (active-LOW, internal pull-up) to
Jetson 40-pin header BOARD-mode pins.

Pin mapping:
  Pin 7  (GPIO216) → Button 1  (A / col 1 / row 1)
  Pin 11 (GPIO50)  → Button 2  (B / col 2 / row 2)
  Pin 13 (GPIO51)  → Button 3  (C / col 3 / row 3)
  Pin 15 (GPIO160) → Button 4  (D / col 4 / row 4)
  Pin 29 (GPIO149) → Button 5  (E / col 5 / row 5)
  Pin 31 (GPIO200) → Button 6  (F / col 6 / row 6)
  Pin 26 (GPIO168) → Button 7  (G / col 7 / row 7)
  Pin 24 (GPIO195) → Button 8  (H / col 8 / row 8)
  Pin 32 (GPIO114) → Button 9  (OK / confirm) - MOVED FROM 19 FOR SPI
  Pin 16 (GPIO163) → HINT button (hardware interrupt, falling edge)

Set MOCK_BUTTONS=1 to run without GPIO hardware (keyboard input in terminal).
"""

import os
import time
import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)

MOCK = os.environ.get("MOCK_BUTTONS", "0") == "1"
DEBOUNCE_S = 0.3  # 300 ms — matches buttonDebounceTime in Arduino sketch

# Jetson BOARD-mode pin numbers for buttons 1–9
BUTTON_PINS = {
    1: 7,  # A/1
    2: 11,  # B/2
    3: 13,  # C/3
    4: 15,  # D/4
    5: 29,  # E/5
    6: 31,  # F/6
    7: 26,  # G/7
    8: 24,  # H/8
    9: 32,  # OK (Moved to 32 to free up SPI MOSI)
}
HINT_PIN = 16  # falling-edge interrupt
BOARD_MODE = 10  # Jetson.GPIO BOARD mode constant

if not MOCK:
    try:
        import Jetson.GPIO as GPIO
    except ImportError:
        log.warning("Jetson.GPIO not found — falling back to mock mode.")
        MOCK = True

if MOCK:
    import queue as _queue
    import threading as _threading

    class _MockGPIO:
        """Keyboard-driven stub: type 1-9 or 'h' + Enter."""

        BOARD = 10
        IN = 1
        PUD_UP = 22
        FALLING = 32

        def __init__(self):
            self._q: _queue.Queue = _queue.Queue()
            self._callbacks: dict = {}
            self._mode = None
            self._start_reader()

        def setmode(self, m):
            self._mode = m

        def getmode(self):
            return self._mode

        def setwarnings(self, w):
            pass

        def cleanup(self):
            log.debug("[MOCK] GPIO.cleanup()")

        def setup(self, pin, direction, pull_up_down=None):
            pass

        def input(self, pin):
            """Non-blocking snapshot — LOW (0) only if queued key matches this pin."""
            if not self._q.empty():
                key = self._q.queue[0]  # peek
                for num, p in BUTTON_PINS.items():
                    if p == pin and str(num) == key:
                        self._q.get_nowait()  # consume
                        return 0  # LOW = pressed
            return 1  # HIGH = not pressed

        def add_event_detect(self, pin, edge, callback=None, bouncetime=200):
            if callback:
                self._callbacks[pin] = callback

        def remove_event_detect(self, pin):
            self._callbacks.pop(pin, None)

        def _start_reader(self):
            def _read():
                print("[MockGPIO] Ready — type 1-9 (buttons) or h (hint), then Enter")
                while True:
                    try:
                        key = input("btn> ").strip().lower()
                        if key == "h":
                            cb = self._callbacks.get(HINT_PIN)
                            if cb:
                                cb(HINT_PIN)
                        else:
                            self._q.put(key)
                    except EOFError:
                        break

            _threading.Thread(target=_read, daemon=True).start()

    GPIO = _MockGPIO()  # type: ignore


class ButtonController:
    def __init__(self):
        # --- FIX FOR ADAFRUIT BLINKA COLLISION ---
        # Blinka globally locks Jetson.GPIO to TEGRA_SOC or BCM mode upon import. 
        # Since the LEDs use hardware SPI (not GPIO), we can safely clear 
        # this lock to enforce our physical BOARD numbering for the buttons.
        current_mode = GPIO.getmode()
        if current_mode is not None and current_mode != BOARD_MODE:
            log.info(f"Clearing conflicting Blinka GPIO mode ({current_mode})...")
            try:
                GPIO.cleanup()
            except Exception as e:
                log.warning(f"GPIO cleanup warning: {e}")

        GPIO.setmode(BOARD_MODE)
        GPIO.setwarnings(False)
        
        for pin in BUTTON_PINS.values():
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(HINT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        
        self._hint_callback: Optional[Callable] = None
        self._last_hint_time = 0.0
        log.info("ButtonController ready")

    # ── Public API ────────────────────────────────────────────────────────────

    def register_hint_callback(self, callback: Callable):
        """
        Attach an interrupt to HINT_PIN (falling edge).
        Mirrors attachInterrupt(digitalPinToInterrupt(3), hint, FALLING).
        """
        self._hint_callback = callback
        GPIO.add_event_detect(
            HINT_PIN,
            GPIO.FALLING,
            callback=self._raw_hint_handler,
            bouncetime=200,
        )

    def detect_button(self) -> int:
        """
        Blocking poll — returns 1-9 when a button is pressed.
        Exact mirror of detectButton() in Arduino sketch.
        10 ms poll interval to avoid busy-spinning.
        """
        while True:
            for num, pin in BUTTON_PINS.items():
                if GPIO.input(pin) == 0:  # LOW = pressed
                    log.debug(f"Button {num} (pin {pin})")
                    time.sleep(DEBOUNCE_S)
                    return num
            time.sleep(0.01)

    def is_ok_held(self) -> bool:
        """True if OK button (btn 9, pin 32) is currently LOW."""
        return GPIO.input(BUTTON_PINS[9]) == 0

    def is_button8_held(self) -> bool:
        """True if button 8 / H (pin 24) is currently LOW."""
        return GPIO.input(BUTTON_PINS[8]) == 0

    def cleanup(self):
        GPIO.remove_event_detect(HINT_PIN)
        GPIO.cleanup()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _raw_hint_handler(self, channel):
        """200 ms software debounce wrapper (mirrors Arduino ISR debounce)."""
        now = time.time()
        if now - self._last_hint_time < 0.2:
            return
        self._last_hint_time = now
        if self._hint_callback:
            self._hint_callback(channel)