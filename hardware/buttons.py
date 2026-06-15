# =============================================================================
# hardware/buttons.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: High-performance button matrix driver for Jetson Orin Nano.
# =============================================================================

from __future__ import annotations

import os
import sys
import time
import logging
import threading
from typing import Callable, Optional

from config import CFG

log = logging.getLogger(__name__)

MOCK = os.environ.get("MOCK_BUTTONS", "0") == "1"

# ── GPIO import & Mock Fallback ───────────────────────────────────────────────

if not MOCK:
    try:
        import Jetson.GPIO as GPIO
    except ImportError:
        log.warning("Jetson.GPIO not found — falling back to mock mode.")
        MOCK = True

if MOCK:
    import queue as _queue

    class _MockGPIO:
        BCM = 11; BOARD = 10; IN = 1; OUT = 0
        HIGH = 1; LOW   = 0

        def __init__(self):
            self._q: _queue.Queue = _queue.Queue()
            self._pins: dict = {}
            self._start_reader()

        def setmode(self, m): pass
        def setwarnings(self, w): pass
        def getmode(self): return self.BOARD
        def cleanup(self): log.debug("[MOCK] GPIO.cleanup()")

        def setup(self, pin, direction, initial=None):
            self._pins[pin] = initial if initial is not None else self.HIGH

        def output(self, pin, val):
            self._pins[pin] = val

        def input(self, pin):
            if not self._q.empty():
                pending = self._q.queue[0]
                col_pin, row_pin = pending
                if self._pins.get(row_pin, self.HIGH) == self.LOW:
                    if pin == col_pin:
                        self._q.get_nowait()
                        return self.LOW
            return self.HIGH

        def _start_reader(self):
            MATRIX = {
                "1": (COL_PINS[1], ROW_PINS[3]),
                "2": (COL_PINS[0], ROW_PINS[3]),
                "3": (COL_PINS[1], ROW_PINS[0]),
                "4": (COL_PINS[0], ROW_PINS[0]),
                "5": (COL_PINS[1], ROW_PINS[4]),
                "6": (COL_PINS[0], ROW_PINS[4]),
                "7": (COL_PINS[1], ROW_PINS[1]),
                "8": (COL_PINS[0], ROW_PINS[1]),
                "9": (COL_PINS[1], ROW_PINS[2]),
                "h": (COL_PINS[0], ROW_PINS[2]),
            }
            def _read():
                print("[MockGPIO] Ready — type 1-9 or h, then Enter")
                while True:
                    try:
                        key = input("btn> ").strip().lower()
                        if key in MATRIX:
                            self._q.put(MATRIX[key])
                    except EOFError:
                        break
            threading.Thread(target=_read, daemon=True).start()

    GPIO = _MockGPIO()  # type: ignore


# ── Validated 2x5 Matrix Wire Mapping ─────────────────────────────────────────

COL_PINS = [7, 11]              
ROW_PINS = [36, 37, 32, 18, 22] 

MATRIX_MAP = {
    1: (3, 1),  # Row Index 3, Col Index 1 -> Button 1
    2: (3, 0),  # Row Index 3, Col Index 0 -> Button 2
    3: (0, 1),  # Row Index 0, Col Index 1 -> Button 3
    4: (0, 0),  # Row Index 0, Col Index 0 -> Button 4
    5: (4, 1),  # Row Index 4, Col Index 1 -> Button 5
    6: (4, 0),  # Row Index 4, Col Index 0 -> Button 6
    7: (1, 1),  # Row Index 1, Col Index 1 -> Button 7
    8: (1, 0),  # Row Index 1, Col Index 0 -> Button 8
    9: (2, 1),  # Row Index 2, Col Index 1 -> Button 9 (OK / Confirm)
}
HINT_POS = (2, 0)   # Row Index 2, Col Index 0 -> Hint Button

_CELL_TO_BTN: dict = {v: k for k, v in MATRIX_MAP.items()}
_CELL_TO_BTN[HINT_POS] = "hint"


class ButtonController:
    CONFIRM_SAMPLES   = 4        
    RELEASE_SAMPLES   = 4        
    SAMPLE_INTERVAL_S = 0.002   

    def __init__(self):
        self._lock = threading.Lock()
        
        GPIO.setwarnings(False)
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                GPIO.cleanup()
        except Exception:
            pass

        GPIO.setmode(GPIO.BOARD)

        for pin in COL_PINS:
            GPIO.setup(pin, GPIO.IN)

        for pin in ROW_PINS:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)

        all_cells = list(MATRIX_MAP.values()) + [HINT_POS]
        self._low_count:  dict = {c: 0  for c in all_cells}
        self._high_count: dict = {c: self.RELEASE_SAMPLES for c in all_cells}
        self._armed:      dict = {c: True  for c in all_cells}

        self._hint_callback: Optional[Callable] = None
        self._last_hint_time = 0.0
        self._hint_poll_running = False
        self._hint_poll_thread: Optional[threading.Thread] = None

        log.info("ButtonController initialized successfully.")

    def register_hint_callback(self, callback: Callable):
        self._hint_callback = callback
        self._hint_poll_running = True
        self._hint_poll_thread = threading.Thread(
            target=self._hint_poll_loop, daemon=True, name="hint-poll"
        )
        self._hint_poll_thread.start()

    def detect_button(self) -> int:
        for cell in MATRIX_MAP.values():
            self._low_count[cell]  = 0
            self._high_count[cell] = self.RELEASE_SAMPLES
            self._armed[cell]      = True

        while True:
            result = self._scan_once()
            if isinstance(result, int):
                print(f" [HARDWARE] Button {result} PRESSED (blocking mode)")
                sys.stdout.flush()
                if not MOCK:
                    while True:
                        pressed = self._scan_matrix()
                        if pressed is None:
                            break
                        time.sleep(0.005)
                print(f" [HARDWARE] Button {result} RELEASED")
                sys.stdout.flush()
                return result
            time.sleep(self.SAMPLE_INTERVAL_S)

    def detect_button_nowait(self) -> int:
        # Rapid single-scan to check for instant contact
        pressed_cell = self._scan_matrix()
        if pressed_cell is None:
            return 0
        
        # Debounce the input here using high-speed sampling
        cell = pressed_cell
        match_count = 1
        for _ in range(3):
            time.sleep(0.002)
            if self._scan_matrix() == cell:
                match_count += 1
                
        if match_count >= 3:
            btn = _CELL_TO_BTN.get(cell)
            if isinstance(btn, int):
                print(f" [HARDWARE] Button {btn} DETECTED (debounced nowait)")
                sys.stdout.flush()
                return btn
        return 0

    def is_ok_held(self) -> bool:
        return self._is_cell_low(*MATRIX_MAP[9])

    def is_button8_held(self) -> bool:
        return self._is_cell_low(*MATRIX_MAP[8])

    def cleanup(self):
        self._hint_poll_running = False
        if self._hint_poll_thread:
            self._hint_poll_thread.join(timeout=1.0)
        with self._lock:
            for pin in ROW_PINS:
                try:
                    GPIO.setup(pin, GPIO.IN)
                except Exception:
                    pass
            GPIO.cleanup()

    def _scan_matrix(self) -> Optional[tuple]:
        with self._lock:
            for row_idx, row_pin in enumerate(ROW_PINS):
                GPIO.output(row_pin, GPIO.LOW)
                for col_idx, col_pin in enumerate(COL_PINS):
                    if GPIO.input(col_pin) == GPIO.LOW:
                        GPIO.output(row_pin, GPIO.HIGH)
                        return (row_idx, col_idx)
                GPIO.output(row_pin, GPIO.HIGH)
            return None

    def _scan_once(self):
        pressed_cell = self._scan_matrix()

        for cell in list(MATRIX_MAP.values()) + [HINT_POS]:
            if pressed_cell == cell:
                self._high_count[cell] = 0
                if self._armed[cell]:
                    self._low_count[cell] += 1
                    if self._low_count[cell] >= self.CONFIRM_SAMPLES:
                        self._low_count[cell] = 0
                        self._armed[cell]     = False
                        return _CELL_TO_BTN.get(cell)
            else:
                self._low_count[cell] = 0
                self._high_count[cell] += 1
                if not self._armed[cell] and self._high_count[cell] >= self.RELEASE_SAMPLES:
                    self._armed[cell] = True
        return None

    def _is_cell_low(self, row_idx: int, col_idx: int, samples: int = 3) -> bool:
        with self._lock:
            row_pin = ROW_PINS[row_idx]
            col_pin = COL_PINS[col_idx]
            GPIO.output(row_pin, GPIO.LOW)
            count = sum(1 for _ in range(samples) if GPIO.input(col_pin) == GPIO.LOW)
            GPIO.output(row_pin, GPIO.HIGH)
            return count >= samples

    def _hint_poll_loop(self):
        filename_cell = HINT_POS
        low_count  = 0
        high_count = self.RELEASE_SAMPLES
        armed      = True

        while self._hint_poll_running:
            pressed_cell = self._scan_matrix()
            if pressed_cell == filename_cell:
                high_count = 0
                if armed:
                    low_count += 1
                    if low_count >= self.CONFIRM_SAMPLES:
                        low_count = 0
                        armed     = False
                        now = time.time()
                        if now - self._last_hint_time >= 0.2:
                            self._last_hint_time = now
                            if self._hint_callback:
                                print(" [HARDWARE] HINT Button Triggered")
                                sys.stdout.flush()
                                threading.Thread(
                                    target=self._hint_callback,
                                    args=(None,),
                                    daemon=True,
                                ).start()
            else:
                low_count = 0
                high_count += 1
                if not armed and high_count >= self.RELEASE_SAMPLES:
                    armed = True
            time.sleep(0.03)