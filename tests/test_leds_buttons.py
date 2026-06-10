#!/usr/bin/env python3
"""
tests/test_leds_buttons.py
Hardware test — direct port of ChessBoardTestLights_Buttons.ino

Run on the Jetson (requires hardware or MOCK_LEDS=1 MOCK_BUTTONS=1):
  sudo python3 tests/test_leds_buttons.py

Or on desktop in mock mode:
  MOCK_LEDS=1 MOCK_BUTTONS=1 python3 tests/test_leds_buttons.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hardware.leds import LEDController
from hardware.buttons import ButtonController

DELAY = 1.0  # seconds between LED test steps

RED = (255, 0, 0)
WHITE = (255, 255, 255)
BLUE = (0, 0, 255)
GREEN = (0, 255, 0)
BLACK = (0, 0, 0)


def main():
    leds = LEDController()
    buttons = ButtonController()

    print("Testing LEDs...")

    # Chessboard — 8 rows in different colours, one row at a time
    colour_sequence = [RED, WHITE, BLUE, GREEN, RED, WHITE, BLUE, GREEN]
    for row_idx, color in enumerate(colour_sequence):
        start_px = row_idx * 8
        leds.chess_fill(color, start=start_px, count=8)
        leds.chess_show()
        time.sleep(DELAY)

    # Control panel sections
    leds.control_panel_fill(WHITE, start=0, count=2)
    leds.panel_show()
    time.sleep(DELAY)

    leds.control_panel_fill(WHITE, start=2, count=2)
    leds.panel_show()
    time.sleep(DELAY)

    leds.control_panel_fill(WHITE, start=4, count=1)
    leds.panel_show()
    time.sleep(DELAY)

    leds.control_panel_fill(WHITE, start=5, count=1)
    leds.panel_show()
    time.sleep(DELAY)

    leds.control_panel_fill(BLUE, start=6, count=8)
    leds.panel_show()
    time.sleep(DELAY)

    leds.control_panel_fill(BLUE, start=14, count=8)
    leds.panel_show()
    time.sleep(DELAY)

    # Everything green — signal start of button test
    leds.control_panel_fill(GREEN, start=0, count=22)
    leds.chess_fill(GREEN)
    leds.show_all()
    time.sleep(DELAY)

    print("All LEDs should now be green.")
    print("Press buttons 1-9 to test. Ctrl+C to exit.")

    btn_names = {
        1: "Button 1 (A/1)",
        2: "Button 2 (B/2)",
        3: "Button 3 (C/3)",
        4: "Button 4 (D/4)",
        5: "Button 5 (E/5)",
        6: "Button 6 (F/6)",
        7: "Button 7 (G/7)",
        8: "Button 8 (H/8)",
        9: "Button 9 (OK)",
    }

    try:
        while True:
            btn = buttons.detect_button()
            print(f"Detected: {btn_names.get(btn, f'Unknown ({btn})')}")
    except KeyboardInterrupt:
        print("\nTest complete.")
        leds.all_off()
        buttons.cleanup()


if __name__ == "__main__":
    main()
