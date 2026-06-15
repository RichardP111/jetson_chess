# =============================================================================
# hardware/buttons.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-14
# Purpose: High-performance background-polling button driver for Jetson Orin Nano.
#          Supports multi-key combination shortcuts via parallel matrix tracking.
# =============================================================================

from __future__ import annotations

import os
import sys
import time
import queue
import logging
import threading
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


COL_PINS = [7, 11]              
ROW_PINS = [36, 37, 32, 18, 22] 

MATRIX_MAP = {
    1: (3, 1),
    2: (3, 0),
    3: (0, 1),
    4: (0, 0),
    5: (4, 1),
    6: (4, 0),
    7: (1, 1),
    8: (1, 0),
    9: (2, 1),
}
HINT_POS = (2, 0)   

_CELL_TO_BTN: dict = {v: k for k, v in MATRIX_MAP.items()}
_CELL_TO_BTN[HINT_POS] = "hint"


class ButtonController:
    CONFIRM_SAMPLES   = 4        
    RELEASE_SAMPLES   = 4        
    SAMPLE_INTERVAL_S = 0.002   

    def __init__(self):
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        
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
        
        # Tracks real-time debounced holding state for each cross-point button
        self._cell_held:  dict = {c: False for c in all_cells}

        self._hint_callback: Optional[Callable] = None
        self._last_hint_time = 0.0
        
        # Launch Master background 500Hz polling loop
        self._running = True
        self._poll_thread = threading.Thread(target=self._master_poll_loop, daemon=True, name="button-poll")
        self._poll_thread.start()

        log.info("ButtonController initialized with multi-key matrix tracking.")

    def register_hint_callback(self, callback: Callable):
        self._hint_callback = callback

    def detect_button(self) -> int:
        """Blocks until a debounced button press event becomes available."""
        return self._queue.get()

    def detect_button_nowait(self) -> int:
        """Instantly returns the next button press in the queue, or 0 if empty."""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return 0

    def _scan_once(self):
        return self.detect_button_nowait()

    def is_ok_held(self) -> bool:
        """Returns the high-speed debounced cached holding state of Button 9."""
        return self._cell_held.get(MATRIX_MAP[9], False)

    def is_button8_held(self) -> bool:
        """Returns the high-speed debounced cached holding state of Button 8."""
        return self._cell_held.get(MATRIX_MAP[8], False)

    def cleanup(self):
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=1.0)
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

    def _master_poll_loop(self):
        all_cells = list(MATRIX_MAP.values()) + [HINT_POS]
        
        while self._running:
            # Step 1: Scan the ENTIRE grid in parallel without breaking early
            current_states = {}
            with self._lock:
                for row_idx, row_pin in enumerate(ROW_PINS):
                    GPIO.output(row_pin, GPIO.LOW)
                    for col_idx, col_pin in enumerate(COL_PINS):
                        current_states[(row_idx, col_idx)] = (GPIO.input(col_pin) == GPIO.LOW)
                    GPIO.output(row_pin, GPIO.HIGH)

            # Step 2: Debounce each button tracking status independently
            for cell in all_cells:
                is_pressed = current_states.get(cell, False)

                if is_pressed:
                    self._high_count[cell] = 0
                    if self._armed[cell]:
                        self._low_count[cell] += 1
                        if self._low_count[cell] >= self.CONFIRM_SAMPLES:
                            self._low_count[cell] = 0
                            self._armed[cell]     = False
                            self._cell_held[cell] = True  # Latch as held
                            
                            btn = _CELL_TO_BTN.get(cell)
                            
                            # Step 3: Handle Double-Sided Combo Interception
                            if btn == "hint":
                                # If HINT is hit second, check if shortcuts are active
                                if self._cell_held[MATRIX_MAP[8]] or self._cell_held[MATRIX_MAP[9]]:
                                    if self._hint_callback:
                                        threading.Thread(target=self._hint_callback, args=(None,), daemon=True).start()
                                else:
                                    now = time.time()
                                    if now - self._last_hint_time >= 0.2:
                                        self._last_hint_time = now
                                        if self._hint_callback:
                                            print(" [HARDWARE] HINT Button Triggered")
                                            sys.stdout.flush()
                                            threading.Thread(target=self._hint_callback, args=(None,), daemon=True).start()
                            
                            elif isinstance(btn, int):
                                # If Button 8 or OK is hit second while HINT is already held down
                                if self._cell_held[HINT_POS] and btn in (8, 9):
                                    if self._hint_callback:
                                        threading.Thread(target=self._hint_callback, args=(None,), daemon=True).start()
                                else:
                                    # Regular button press queue routing
                                    print(f" [HARDWARE] Button {btn} CAPTURED (Background Thread)")
                                    sys.stdout.flush()
                                    self._queue.put(btn)
                else:
                    self._low_count[cell] = 0
                    self._high_count[cell] += 1
                    if not self._armed[cell] and self._high_count[cell] >= self.RELEASE_SAMPLES:
                        self._armed[cell] = True
                        self._cell_held[cell] = False  # Clear held latch
                        
            time.sleep(self.SAMPLE_INTERVAL_S)