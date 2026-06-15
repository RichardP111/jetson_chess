#!/usr/bin/env python3
"""
Coordinate mapping test.
Press each button and it tells you what chess square coordinate it would produce.
This lets you verify the mapping is correct before playing.
"""
import sys, os, time
sys.path.insert(0, '/home/jetson/jetson_chess')

from hardware.buttons import ButtonController

COL_MAP = {1:"a", 2:"b", 3:"c", 4:"d", 5:"e", 6:"f", 7:"g", 8:"h"}
ROW_MAP = {1:"1", 2:"2", 3:"3", 4:"4", 5:"5", 6:"6", 7:"7", 8:"8"}

print("=" * 50)
print("COORDINATE MAPPING TEST")
print("=" * 50)
print("This test shows what chess coordinate each")
print("button produces when used as col then row.")
print()
print("For COLUMN input: btn1=A, btn2=B ... btn8=H")
print("For ROW input:    btn1=1, btn2=2 ... btn8=8")
print()
print("Press buttons one at a time. Ctrl+C to quit.")
print("-" * 50)

buttons = ButtonController()

try:
    while True:
        btn = buttons.detect_button()
        col = COL_MAP.get(btn, "?")
        row = ROW_MAP.get(btn, "?")
        print(f"  Button {btn} → as column: {col.upper()}  |  as row: {row}")
        print(f"           → example squares: {col}1, {col}4, {col}8")
        print()
except KeyboardInterrupt:
    print("\nDone.")
    buttons.cleanup()