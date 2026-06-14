# =============================================================================
# tests/test_leds_buttons.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-14
# Purpose: Hardware integration test for 2×5 button matrix + LED strips.
#          Run with MOCK_LEDS=1 MOCK_BUTTONS=1 for desktop use.
# =============================================================================

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hardware.leds    import LEDController
from hardware.buttons import ButtonController, COL_PINS, ROW_PINS, MATRIX_MAP, HINT_POS

DELAY = 0.6

RED    = (255,   0,   0)
GREEN  = (  0, 255,   0)
BLUE   = (  0,   0, 255)
WHITE  = (255, 255, 255)
YELLOW = (255, 200,   0)
PURPLE = (160,   0, 255)
BLACK  = (  0,   0,   0)


def section(title: str):
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print(f"{'─'*50}")


def main():
    # ButtonController MUST init before LEDController so BOARD mode is claimed
    # before rpi_ws281x tries to set BCM mode.
    print("Initialising ButtonController...")
    buttons = ButtonController()
    print("Initialising LEDController...")
    leds = LEDController()

    # ── LED TEST ──────────────────────────────────────────────────────────────
    section("LED TEST — Chessboard")

    colours = [RED, WHITE, BLUE, GREEN, YELLOW, PURPLE, WHITE, RED]
    for row_idx, colour in enumerate(colours):
        leds.chess_fill(colour, start=row_idx * 8, count=8)
        leds.chess_show()
        time.sleep(DELAY)
    print("  8 rows lit in different colours — check board")

    section("LED TEST — Full board sweep")
    for colour in [RED, GREEN, BLUE, WHITE]:
        leds.chess_fill(colour)
        leds.chess_show()
        time.sleep(DELAY)
    leds.chess_fill(BLACK)
    leds.chess_show()
    print("  Red → Green → Blue → White sweep done")

    section("LED TEST — Control panel")
    leds.control_panel_fill(WHITE, start=0, count=2)
    leds.panel_show(); time.sleep(DELAY)
    leds.control_panel_fill(WHITE, start=2, count=2)
    leds.panel_show(); time.sleep(DELAY)
    leds.control_panel_fill(WHITE, start=4, count=1)
    leds.panel_show(); time.sleep(DELAY)
    leds.control_panel_fill(WHITE, start=5, count=1)
    leds.panel_show(); time.sleep(DELAY)
    leds.control_panel_fill(BLUE, start=6, count=8)
    leds.panel_show(); time.sleep(DELAY)
    leds.control_panel_fill(BLUE, start=14, count=8)
    leds.panel_show(); time.sleep(DELAY)
    print("  Control panel sections lit — check panel")

    # All green = ready for button test
    leds.chess_fill(GREEN)
    leds.chess_show()
    leds.control_panel_fill(GREEN, start=0, count=22)
    leds.panel_show()
    print("\n  All LEDs green — starting button test")
    time.sleep(0.5)

    # ── MATRIX INFO ───────────────────────────────────────────────────────────
    section("BUTTON MATRIX INFO")
    print(f"  Columns : {COL_PINS}  (pin 7 = 470Ω pull-up, pin 11 = internal)")
    print(f"  Rows    : {ROW_PINS}")
    print()
    print(f"  {'BTN':<6} {'(row,col)':<12} {'Row pin':<10} {'Col pin'}")
    print(f"  {'───':<6} {'─────────':<12} {'───────':<10} {'───────'}")
    for btn, (ri, ci) in sorted(MATRIX_MAP.items()):
        label = "OK" if btn == 9 else str(btn)
        print(f"  btn{label:<4} ({ri},{ci})        {ROW_PINS[ri]:<10} {COL_PINS[ci]}")
    hi, ci2 = HINT_POS
    print(f"  hint   ({hi},{ci2})        {ROW_PINS[hi]:<10} {COL_PINS[ci2]}")

    # ── BUTTON TEST ───────────────────────────────────────────────────────────
    section("BUTTON TEST — press each button")

    BTN_NAMES = {
        1: "Button 1", 2: "Button 2", 3: "Button 3",
        4: "Button 4", 5: "Button 5", 6: "Button 6",
        7: "Button 7", 8: "Button 8", 9: "Button 9 (OK)",
    }

    # LED colour per button for visual feedback
    BTN_COLOURS = {
        1: RED,    2: GREEN,  3: BLUE,   4: YELLOW,
        5: PURPLE, 6: WHITE,  7: RED,    8: GREEN,
        9: WHITE,
    }

    # Which chess board square to light per button (col 0-7, row 0-7)
    BTN_SQUARE = {
        1: (0,0), 2: (1,1), 3: (2,2), 4: (3,3),
        5: (4,4), 6: (5,5), 7: (6,6), 8: (7,7),
        9: (3,3),
    }

    pressed_set = set()
    total = len(MATRIX_MAP)  # 9 chess buttons (hint tested separately)

    print(f"  Press all {total} buttons + HINT. Board flashes on each press.")
    print("  Ctrl+C to skip to hint test.\n")

    try:
        while len(pressed_set) < total:
            btn = buttons.detect_button()
            name = BTN_NAMES.get(btn, f"Unknown({btn})")
            is_new = btn not in pressed_set
            pressed_set.add(btn)
            col, row = BTN_SQUARE.get(btn, (0, 0))
            colour = BTN_COLOURS.get(btn, WHITE)

            # Flash the corresponding square
            leds.chess_set_pixel(col, row, colour)
            leds.chess_show()
            time.sleep(0.15)
            leds.chess_set_pixel(col, row, BLACK)
            leds.chess_show()

            status = "NEW ✓" if is_new else "repeat"
            remaining = total - len(pressed_set)
            print(f"  [{status}] {name}  —  {remaining} remaining")

        print("\n  ✓ All 9 buttons confirmed!")

    except KeyboardInterrupt:
        print(f"\n  Skipped — {len(pressed_set)}/{total} confirmed: {sorted(pressed_set)}")

    # ── HINT BUTTON TEST ──────────────────────────────────────────────────────
    section("HINT BUTTON TEST")
    print("  Press HINT button 3 times (it runs in a background thread)...")

    hint_count = [0]

    def on_hint(_):
        hint_count[0] += 1
        leds.chess_fill(YELLOW)
        leds.chess_show()
        time.sleep(0.2)
        leds.chess_fill(BLACK)
        leds.chess_show()
        print(f"  HINT press {hint_count[0]} detected!")

    buttons.register_hint_callback(on_hint)

    deadline = time.time() + 10.0
    while hint_count[0] < 3 and time.time() < deadline:
        time.sleep(0.1)

    if hint_count[0] >= 3:
        print("  ✓ Hint button working!")
    else:
        print(f"  ✗ Only {hint_count[0]}/3 hint presses detected in 10s")

    # ── DONE ──────────────────────────────────────────────────────────────────
    section("TEST COMPLETE")
    leds.chess_fill(GREEN if len(pressed_set) == total else RED)
    leds.chess_show()
    time.sleep(1.0)
    leds.all_off()
    buttons.cleanup()
    print("  Goodbye!\n")


if __name__ == "__main__":
    main()