# =============================================================================
# hardware/buttons.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: Button input via Jetson Orin Nano GPIO using confirmed BCM
#          (line_offset) numbers for this board. See config.py for the
#          full BOARD->BCM mapping table.
#
#          rpi_ws281x unconditionally sets GPIO.BCM when the LED strip
#          initialises — we align with that and use BCM numbers throughout.
#
#          No-resistor debounce: Jetson.GPIO ignores pull_up_down, so pins
#          float. Three consecutive LOW reads 5 ms apart reject noise and
#          confirm a real press. Permanent fix: 10 kΩ from each pin to 3.3 V.
#
#          Set MOCK_BUTTONS=1 to run without hardware (keyboard input).
# =============================================================================

import os
import time
import logging
import threading
from typing import Callable, Dict, Optional

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

    class _MockGPIO:
        BCM = 11; BOARD = 10; IN = "IN"; FALLING = "FALLING"

        def __init__(self):
            self._q = _queue.Queue()
            self._callbacks: dict = {}
            self._start_reader()

        def setmode(self, m): pass
        def setwarnings(self, w): pass
        def getmode(self): return self.BCM
        def cleanup(self): log.debug("[MOCK] GPIO.cleanup()")
        def setup(self, pin, direction, **kw): pass

        def input(self, pin):
            if not self._q.empty():
                key = self._q.queue[0]
                for num, p in CFG.button_pins.items():
                    if p == pin and str(num) == key:
                        self._q.get_nowait()
                        return 0
            return 1

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
            threading.Thread(target=_read, daemon=True).start()

    GPIO = _MockGPIO()  # type: ignore


class ButtonController:
    """
    Software-debounced button driver using confirmed BCM line_offset numbers.

    Uses GPIO.BCM throughout (rpi_ws281x always sets this mode first).
    Pin numbers in CFG.button_pins are BCM line_offset values confirmed
    by reading Jetson.GPIO's ChannelInfo.line_offset on this exact board.

    Debounce: CONFIRM_SAMPLES consecutive LOW reads before press accepted.
              RELEASE_SAMPLES consecutive HIGH reads before re-arm.
    """

    CONFIRM_SAMPLES   = 3
    RELEASE_SAMPLES   = 5
    SAMPLE_INTERVAL_S = 0.005  # 5 ms → ~15 ms confirmation latency

    def __init__(self):
        GPIO.setwarnings(False)

        # Accept BCM mode whether rpi_ws281x already set it or not
        try:
            current = GPIO.getmode()
        except Exception:
            current = None

        if current is None:
            GPIO.setmode(GPIO.BCM)
        # else: already BCM (11 or Jetson's internal 1000) — nothing to do

        # Setup all pins — no pull_up_down (Jetson ignores it anyway)
        for btn_num, bcm_pin in CFG.button_pins.items():
            try:
                GPIO.setup(bcm_pin, GPIO.IN)
            except Exception as e:
                log.error(f"GPIO.setup failed for button {btn_num} BCM pin {bcm_pin}: {e}")
                raise

        try:
            GPIO.setup(CFG.hint_pin, GPIO.IN)
        except Exception as e:
            log.error(f"GPIO.setup failed for hint BCM pin {CFG.hint_pin}: {e}")
            raise

        self._hint_callback: Optional[Callable] = None
        self._last_hint_time = 0.0

        all_pins = list(CFG.button_pins.values()) + [CFG.hint_pin]
        self._low_count:  Dict[int, int]  = {p: 0 for p in all_pins}
        self._high_count: Dict[int, int]  = {p: self.RELEASE_SAMPLES for p in all_pins}
        self._armed:      Dict[int, bool] = {p: True for p in all_pins}

        self._hint_poll_running = False
        self._hint_poll_thread: Optional[threading.Thread] = None

        log.info(
            f"ButtonController ready — BCM mode, "
            f"debounce {self.CONFIRM_SAMPLES}×{self.SAMPLE_INTERVAL_S*1000:.0f}ms. "
            "Add 10kΩ pull-ups to each pin for hardware-clean inputs."
        )

    # ── Public API ────────────────────────────────────────────────────────

    def register_hint_callback(self, callback: Callable):
        self._hint_callback = callback
        self._hint_poll_running = True
        self._hint_poll_thread = threading.Thread(
            target=self._hint_poll_loop, daemon=True, name="hint-poll"
        )
        self._hint_poll_thread.start()

    def detect_button(self) -> int:
        """Block until a button press is confirmed. Returns 1–9."""
        for pin in CFG.button_pins.values():
            self._low_count[pin]  = 0
            self._high_count[pin] = self.RELEASE_SAMPLES
            self._armed[pin]      = True

        while True:
            for num, pin in CFG.button_pins.items():
                if self._sample_pin(pin) == "pressed":
                    log.debug(f"Button {num} (BCM {pin}) confirmed")
                    return num
            time.sleep(self.SAMPLE_INTERVAL_S)

    def is_ok_held(self) -> bool:
        return self._is_solidly_low(CFG.button_pins[9])

    def is_button8_held(self) -> bool:
        return self._is_solidly_low(CFG.button_pins[8])

    def cleanup(self):
        self._hint_poll_running = False
        if self._hint_poll_thread:
            self._hint_poll_thread.join(timeout=1.0)
        GPIO.cleanup()

    # ── Internal ──────────────────────────────────────────────────────────

    def _sample_pin(self, pin: int) -> str:
        raw = GPIO.input(pin)
        if raw == 0:
            self._high_count[pin] = 0
            if self._armed[pin]:
                self._low_count[pin] += 1
                if self._low_count[pin] >= self.CONFIRM_SAMPLES:
                    self._low_count[pin] = 0
                    self._armed[pin]     = False
                    return "pressed"
        else:
            self._low_count[pin] = 0
            self._high_count[pin] += 1
            if not self._armed[pin] and self._high_count[pin] >= self.RELEASE_SAMPLES:
                self._armed[pin] = True
        return "idle"

    def _is_solidly_low(self, pin: int, samples: int = 3) -> bool:
        for _ in range(samples):
            if GPIO.input(pin) != 0:
                return False
            time.sleep(self.SAMPLE_INTERVAL_S)
        return True

    def _hint_poll_loop(self):
        hint_low = 0; hint_high = self.RELEASE_SAMPLES; hint_armed = True
        while self._hint_poll_running:
            raw = GPIO.input(CFG.hint_pin)
            if raw == 0:
                hint_high = 0
                if hint_armed:
                    hint_low += 1
                    if hint_low >= self.CONFIRM_SAMPLES:
                        hint_low = 0; hint_armed = False
                        now = time.time()
                        if now - self._last_hint_time >= 0.2:
                            self._last_hint_time = now
                            if self._hint_callback:
                                threading.Thread(
                                    target=self._hint_callback,
                                    args=(CFG.hint_pin,),
                                    daemon=True,
                                ).start()
            else:
                hint_low = 0; hint_high += 1
                if not hint_armed and hint_high >= self.RELEASE_SAMPLES:
                    hint_armed = True
            time.sleep(self.SAMPLE_INTERVAL_S)