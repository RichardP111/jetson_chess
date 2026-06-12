# =============================================================================
# hardware/buttons.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: Button input via Jetson Orin Nano GPIO. Maps 10 active-LOW buttons
#          to 40-pin header BOARD-mode pins, with interrupt-driven hint button
#          and debounce matching the original Arduino sketch.
#          Set MOCK_BUTTONS=1 to run without GPIO hardware (terminal input).
# =============================================================================

import os
import time
import logging
from typing import Callable, Optional

from config import CFG

log = logging.getLogger(__name__)

MOCK = os.environ.get("MOCK_BUTTONS", "0") == "1"

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

        BOARD   = "BOARD"
        IN      = "IN"
        PUD_UP  = "PUD_UP"
        FALLING = "FALLING"

        def __init__(self):
            self._q: _queue.Queue = _queue.Queue()
            self._callbacks: dict = {}
            self._start_reader()

        def setmode(self, m):
            pass

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
                for num, p in CFG.button_pins.items():
                    if p == pin and str(num) == key:
                        self._q.get_nowait()  # consume
                        return 0              # LOW = pressed
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
                            cb = self._callbacks.get(CFG.hint_pin)
                            if cb:
                                cb(CFG.hint_pin)
                        else:
                            self._q.put(key)
                    except EOFError:
                        break

            _threading.Thread(target=_read, daemon=True).start()

    GPIO = _MockGPIO()  # type: ignore


class ButtonController:
    def __init__(self):
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        for pin in CFG.button_pins.values():
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(CFG.hint_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self._hint_callback: Optional[Callable] = None
        self._last_hint_time = 0.0
        log.info("ButtonController ready")

    # ── Public API ────────────────────────────────────────────────────────

    def register_hint_callback(self, callback: Callable):
        """
        Attach an interrupt to hint_pin (falling edge).
        Mirrors attachInterrupt(digitalPinToInterrupt(3), hint, FALLING).
        """
        self._hint_callback = callback
        GPIO.add_event_detect(
            CFG.hint_pin,
            GPIO.FALLING,
            callback=self._raw_hint_handler,
            bouncetime=200,
        )

    def detect_button(self) -> int:
        """
        Blocking poll — returns 1-9 when a button is pressed.
        10 ms poll interval to avoid busy-spinning.
        """
        while True:
            for num, pin in CFG.button_pins.items():
                if GPIO.input(pin) == 0:  # LOW = pressed
                    log.debug(f"Button {num} (pin {pin})")
                    time.sleep(CFG.button_debounce_s)
                    return num
            time.sleep(0.01)

    def is_ok_held(self) -> bool:
        """True if OK button (btn 9) is currently LOW."""
        return GPIO.input(CFG.button_pins[9]) == 0

    def is_button8_held(self) -> bool:
        """True if button 8 / H is currently LOW."""
        return GPIO.input(CFG.button_pins[8]) == 0

    def cleanup(self):
        GPIO.remove_event_detect(CFG.hint_pin)
        GPIO.cleanup()

    # ── Internal ──────────────────────────────────────────────────────────

    def _raw_hint_handler(self, channel):
        """200 ms software debounce wrapper."""
        now = time.time()
        if now - self._last_hint_time < 0.2:
            return
        self._last_hint_time = now
        if self._hint_callback:
            self._hint_callback(channel)
