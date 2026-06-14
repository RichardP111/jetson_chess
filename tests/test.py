#!/usr/bin/env python3
"""
Quick matrix button test — 2 cols x 5 rows
Col_1 (pin 7)  — 470Ω to 3.3V
Col_2 (pin 11) — internal pull-up

         Col_1(7)  Col_2(11)
Row_1(36)  btn4      btn3
Row_2(37)  btn8      btn7
Row_3(32)  btn9(OK)  hint
Row_4(18)  btn2      btn1
Row_5(22)  btn6      btn5
"""
import warnings, time
import Jetson.GPIO as GPIO

COL_PINS = [7, 11]
ROW_PINS = [36, 37, 32, 18, 22]

MATRIX = {
    (0,0):"btn4", (0,1):"btn3",
    (1,0):"btn8", (1,1):"btn7",
    (2,0):"HINT",    (2,1):"btn9(OK)",
    (3,0):"btn2", (3,1):"btn1",
    (4,0):"btn6", (4,1):"btn5",
}

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    GPIO.cleanup()

GPIO.setmode(GPIO.BOARD)
for pin in COL_PINS:
    GPIO.setup(pin, GPIO.IN)
for pin in ROW_PINS:
    GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)

print("Matrix test ready — press buttons (Ctrl+C to quit)")
print(f"Cols: {COL_PINS}  Rows: {ROW_PINS}\n")

last = None
try:
    while True:
        pressed = None
        for ri, row_pin in enumerate(ROW_PINS):
            GPIO.output(row_pin, GPIO.LOW)
            for ci, col_pin in enumerate(COL_PINS):
                if GPIO.input(col_pin) == GPIO.LOW:
                    pressed = (ri, ci)
            GPIO.output(row_pin, GPIO.HIGH)

        if pressed and pressed != last:
            name = MATRIX.get(pressed, f"?({pressed})")
            print(f"PRESSED: {name}  (row={ROW_PINS[pressed[0]]} col={COL_PINS[pressed[1]]})")
            last = pressed
        elif not pressed:
            last = None

        time.sleep(0.005)

except KeyboardInterrupt:
    print("\nDone.")
finally:
    GPIO.cleanup()