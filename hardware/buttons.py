# =============================================================================
# hardware/buttons.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-14
# Purpose: Button input via Jetson Orin Nano GPIO — BOARD mode.
#
#          Hardware: 2-column × 5-row matrix.
#          Col_1 (pin 7)  has a 470Ω pull-up resistor to 3.3V.
#          Col_2 (pin 11) has an internal pull-up — no resistor needed.
#          Rows are driven LOW one at a time to scan.
#          Pressed = column reads LOW when its row is driven LOW.
#
#          Matrix layout:
#            Col_1(7)  Col_2(11)
#  Row_1(36)  btn4      btn3
#  Row_2(37)  btn8      btn7
#  Row_3(32)  btn9(OK)  hint
#  Row_4(18)  btn2      btn1
#  Row_5(22)  btn6      btn5
#
#          Hint button: Row_3 Col_2 (pin 11 / row pin 32).
#          OK button  : btn9 Row_3 Col_1 (pin 7 / row pin 32).
#
#          Software debounce: CONFIRM_SAMPLES consecutive LOW reads to
#          confirm press; RELEASE_SAMPLES consecutive HIGH reads to re-arm.
#
#          Set MOCK_BUTTONS=1 to run without hardware (keyboard input).
# =============================================================================

from __future__ import annotations

import os
import time
import logging
import threading
from typing import Callable, Optional

from config import CFG

log = logging.getLogger(__name__)

MOCK = os.environ.get("MOCK_BUTTONS", "0") == "1"

# ── GPIO import ───────────────────────────────────────────────────────────────

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
            # When a row is LOW (being scanned) and a key matches, return LOW
            if not self._q.empty():
                pending = self._q.queue[0]
                col_pin, row_pin = pending
                # Is this row currently driven LOW?
                if self._pins.get(row_pin, self.HIGH) == self.LOW:
                    if pin == col_pin:
                        return self.LOW
            return self.HIGH

        def _consume(self):
            """Remove top of queue — call after full matrix scan confirms press."""
            if not self._q.empty():
                self._q.get_nowait()

        def _start_reader(self):
            MATRIX = {
                "1": (COL_PINS[0], ROW_PINS[0]),
                "2": (COL_PINS[1], ROW_PINS[0]),
                "3": (COL_PINS[2], ROW_PINS[0]),
                "4": (COL_PINS[3], ROW_PINS[0]),
                "5": (COL_PINS[0], ROW_PINS[1]),
                "6": (COL_PINS[1], ROW_PINS[1]),
                "7": (COL_PINS[2], ROW_PINS[1]),
                "8": (COL_PINS[3], ROW_PINS[1]),
                "9": (COL_PINS[0], ROW_PINS[2]),
                "h": (COL_PINS[1], ROW_PINS[2]),
            }
            def _read():
                print("[MockGPIO] Ready — type 1-9 (buttons) or h (hint), then Enter")
                while True:
                    try:
                        key = input("btn> ").strip().lower()
                        if key in MATRIX:
                            self._q.put(MATRIX[key])
                    except EOFError:
                        break
            threading.Thread(target=_read, daemon=True).start()

    GPIO = _MockGPIO()  # type: ignore


# ── Matrix pin definitions ────────────────────────────────────────────────────
#
# Physical BOARD pin numbers (from config, kept in sync).
# Columns have 470Ω pull-ups to 3.3V → read HIGH at rest.
# Rows are output pins, driven LOW to scan.

COL_PINS = [7, 11]              # Col_1 (470Ω to 3.3V), Col_2 (internal pull-up)
ROW_PINS = [36, 37, 32, 18, 22] # Row_1 … Row_5

# Button number → (row_idx, col_idx) in the matrix
#
#          Col_1(7)  Col_2(11)
# Row_1(36)  btn3      btn4      ← your "Button 3" and "Button 4"
# Row_2(37)  btn7      btn8
# Row_3(32)  btn9(OK)  hint
# Row_4(18)  btn1      btn2
# Row_5(22)  btn5      btn6
#
# Mapped so btn1-9 match the chess column/row input order:
MATRIX_MAP = {
    1: (3, 1),  # Row_4 Col_2
    2: (3, 0),  # Row_4 Col_1
    3: (0, 1),  # Row_1 Col_2
    4: (0, 0),  # Row_1 Col_1
    5: (4, 1),  # Row_5 Col_2
    6: (4, 0),  # Row_5 Col_1
    7: (1, 1),  # Row_2 Col_2
    8: (1, 0),  # Row_2 Col_1
    9: (2, 1),  # Row_3 Col_2  (OK / confirm) ← swapped
}
HINT_POS = (2, 0)   # Row_3 Col_1 ← swapped

# Reverse map: (row_idx, col_idx) → button number or "hint"
_CELL_TO_BTN: dict = {v: k for k, v in MATRIX_MAP.items()}
_CELL_TO_BTN[HINT_POS] = "hint"


class ButtonController:
    """
    Matrix button driver for Jetson Orin Nano.

    Scans a 4-col × 3-row matrix.  Columns have external pull-ups (470Ω to
    3.3V) so they rest HIGH.  Rows are output pins driven LOW to select.
    A pressed button pulls its column LOW while its row is LOW.

    Debounce: CONFIRM_SAMPLES consecutive LOW reads to confirm press.
              RELEASE_SAMPLES consecutive HIGH reads to re-arm.
    """

    CONFIRM_SAMPLES   = 3
    RELEASE_SAMPLES   = 5
    SAMPLE_INTERVAL_S = 0.005   # 5 ms per scan cycle

    def __init__(self):
        GPIO.setwarnings(False)

        # Clear any stale GPIO state
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                GPIO.cleanup()
        except Exception:
            pass

        GPIO.setmode(GPIO.BOARD)

        # Set up column pins as inputs (pull-ups are external resistors)
        for pin in COL_PINS:
            try:
                GPIO.setup(pin, GPIO.IN)
            except Exception as e:
                log.error(f"GPIO.setup failed for col pin {pin}: {e}")
                raise

        # Set up row pins as outputs, initially HIGH (inactive)
        for pin in ROW_PINS:
            try:
                GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
            except Exception as e:
                log.error(f"GPIO.setup failed for row pin {pin}: {e}")
                raise

        # Per-cell debounce state: keyed by (row_idx, col_idx)
        all_cells = list(MATRIX_MAP.values()) + [HINT_POS]
        self._low_count:  dict = {c: 0  for c in all_cells}
        self._high_count: dict = {c: self.RELEASE_SAMPLES for c in all_cells}
        self._armed:      dict = {c: True  for c in all_cells}

        self._hint_callback: Optional[Callable] = None
        self._last_hint_time = 0.0
        self._hint_poll_running = False
        self._hint_poll_thread: Optional[threading.Thread] = None

        log.info(
            f"ButtonController ready — {len(COL_PINS)}×{len(ROW_PINS)} matrix, "
            f"cols={COL_PINS}, rows={ROW_PINS}, "
            f"debounce {self.CONFIRM_SAMPLES}×{self.SAMPLE_INTERVAL_S*1000:.0f}ms"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def register_hint_callback(self, callback: Callable):
        self._hint_callback = callback
        self._hint_poll_running = True
        self._hint_poll_thread = threading.Thread(
            target=self._hint_poll_loop, daemon=True, name="hint-poll"
        )
        self._hint_poll_thread.start()

    def detect_button(self) -> int:
        """Block until a confirmed button press (1-9). Never returns 'hint'."""
        # Reset debounce state for button cells only
        for cell in MATRIX_MAP.values():
            self._low_count[cell]  = 0
            self._high_count[cell] = self.RELEASE_SAMPLES
            self._armed[cell]      = True

        while True:
            result = self._scan_once()
            if isinstance(result, int):
                return result
            time.sleep(self.SAMPLE_INTERVAL_S)

    def detect_button_nowait(self) -> int:
        """Non-blocking scan. Returns button number (1-9) if pressed, else 0."""
        result = self._scan_once()
        return result if isinstance(result, int) else 0

    def is_ok_held(self) -> bool:
        """Return True if button 9 (OK) is currently held LOW."""
        return self._is_cell_low(*MATRIX_MAP[9])

    def is_button8_held(self) -> bool:
        """Return True if button 8 is currently held LOW."""
        return self._is_cell_low(*MATRIX_MAP[8])

    def cleanup(self):
        self._hint_poll_running = False
        if self._hint_poll_thread:
            self._hint_poll_thread.join(timeout=1.0)
        # Restore rows to input before cleanup to avoid driving anything
        for pin in ROW_PINS:
            try:
                GPIO.setup(pin, GPIO.IN)
            except Exception:
                pass
        GPIO.cleanup()

    # ── Internal scanning ─────────────────────────────────────────────────────

    def _scan_matrix(self) -> Optional[tuple]:
        """
        Drive each row LOW in turn and read all columns.
        Returns (row_idx, col_idx) of the first pressed cell, or None.
        """
        for row_idx, row_pin in enumerate(ROW_PINS):
            GPIO.output(row_pin, GPIO.LOW)
            time.sleep(0.0005)   # 0.5ms settle time
            for col_idx, col_pin in enumerate(COL_PINS):
                if GPIO.input(col_pin) == GPIO.LOW:
                    GPIO.output(row_pin, GPIO.HIGH)
                    return (row_idx, col_idx)
            GPIO.output(row_pin, GPIO.HIGH)
        return None

    def _scan_once(self):
        """
        Scan the matrix and apply debounce.
        Returns int button number, 'hint', or None.
        """
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
        """Read a specific cell without debounce (for hold detection)."""
        row_pin = ROW_PINS[row_idx]
        col_pin = COL_PINS[col_idx]
        GPIO.output(row_pin, GPIO.LOW)
        time.sleep(0.0005)
        count = sum(1 for _ in range(samples) if GPIO.input(col_pin) == GPIO.LOW)
        GPIO.output(row_pin, GPIO.HIGH)
        return count >= samples

    def _hint_poll_loop(self):
        """Background thread polling the hint button cell."""
        cell = HINT_POS
        low_count  = 0
        high_count = self.RELEASE_SAMPLES
        armed      = True

        while self._hint_poll_running:
            pressed_cell = self._scan_matrix()
            if pressed_cell == cell:
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
            time.sleep(self.SAMPLE_INTERVAL_S)