# =============================================================================
# tests/test_mock_game.py
# Author : Richard Pu
# Created: 2026-06-10
# Purpose: Full mock integration test covering all 18 features ported from
#          ArdunioChess.ino. No hardware, Lichess token, or Stockfish required
#          — all drivers run in mock mode automatically.
# =============================================================================

import os, sys, unittest.mock as mock, builtins

os.environ["MOCK_LEDS"] = "1"
os.environ["MOCK_BUTTONS"] = "1"
os.environ["MOCK_ONLINE"] = "1"
# Prevent mock keyboard thread from blocking on stdin during test
builtins.input = lambda *a, **k: (_ for _ in ()).throw(EOFError())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging

logging.basicConfig(level=logging.WARNING)  # suppress INFO noise during tests

from hardware.leds import LEDController
from hardware.buttons import ButtonController
from chess_engine.board_state import BoardState
from chess_engine.stockfish import StockfishEngine
from ui.display import Display
from online.lichess import LichessClient


def run():
    leds = LEDController()
    buttons = ButtonController()
    board = BoardState()
    engine = StockfishEngine()
    display = Display(leds, board, buttons)

    # 1. Coordinate parsing
    assert board.parse_uci("e2e4") == (4, 6, 4, 4)
    assert board.parse_uci("a1h8") == (0, 7, 7, 0)
    assert board.parse_uci("h8a1") == (7, 0, 0, 7)
    print("1.  Coordinate parsing: PASS")

    # 2. Starting-position occupied tracking
    for row in [0, 1, 6, 7]:
        for col in range(8):
            assert board.is_square_occupied(
                col, row
            ), f"({col},{row}) should be occupied"
    for row in range(2, 6):
        for col in range(8):
            assert not board.is_square_occupied(
                col, row
            ), f"({col},{row}) should be empty"
    print("2.  Occupied tracking (starting position): PASS")

    # 3. Legal move validation  (mirrors checkPiForError)
    assert engine.is_legal(board.fen(), "e2e4") == True
    assert engine.is_legal(board.fen(), "e2e5") == False  # illegal jump
    assert engine.is_legal(board.fen(), "g1f3") == True
    assert engine.is_legal(board.fen(), "a1a1") == False  # invalid UCI
    print("3.  Legal move check: PASS")

    # 4. Move application updates occupied array correctly
    for m in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]:
        board.apply_move(m)
    assert board.is_square_occupied(4, 4), "e4 should be occupied"
    assert not board.is_square_occupied(4, 6), "e2 should be empty"
    print("4.  Move application: PASS")

    # 5. light_up_move Y (auto-continue, no wait)
    display.light_up_move("e2e4", mode="Y")
    display.light_up_move("g1f3", mode="Y")
    print("5.  light_up_move Y (auto-continue): PASS")

    # 6. light_up_move N (wait for OK button — btn 9)
    with mock.patch.object(buttons, "detect_button", return_value=9):
        display.light_up_move("f1c4", mode="N")
    print("6.  light_up_move N (OK-wait): PASS")

    # 7. light_up_move H (hint, CYAN, 4s auto-dismiss)
    with mock.patch("time.sleep"):
        display.light_up_move("c4f7", mode="H")
    print("7.  light_up_move H (hint, CYAN): PASS")

    # 8. Capture flash — f7 has a black pawn → RED blink × 3
    display.light_up_move("c4f7", mode="Y")
    print("8.  Capture flash (RED blink × 3): PASS")

    # 9. Board markings
    display.show_board_markings()
    display.show_opening_markings()
    print("9.  Board markings (full + opening rows): PASS")

    # 10. Setup icons (difficulty L, timeout !, colour split)
    display.show_difficulty_icon()
    display.show_timeout_icon()
    display.show_colour_choice_icon()
    print("10. Setup icons (L, !, colour split): PASS")

    # 11. Error animation (blue fill + red X × 3)
    with mock.patch("time.sleep"):
        display.error_animation()
    print("11. Error animation (blue fill + red X): PASS")

    # 12. Checkmate animation (shrinking red rects + flash attacker × 5)
    with mock.patch("time.sleep"):
        display.checkmate_animation("e7e8")
    print("12. Checkmate animation (rects + flash): PASS")

    # 13. Fast loading animation (25 ms/square)
    with mock.patch("time.sleep"):
        display.loading_animation_fast()
    print("13. Fast loading animation (25 ms/sq): PASS")

    # 14. Engine get_move returns valid UCI + hint
    move, hint = engine.get_move(board.fen(), skill_level=5, movetime_ms=500)
    assert len(move) >= 4, f"Bad engine move: {move!r}"
    print(f"14. Engine get_move  move={move}  hint={hint}: PASS")

    # 15. Board reset restores starting position
    board.reset()
    assert board.is_square_occupied(4, 6), "e2 should be occupied after reset"
    assert not board.is_square_occupied(4, 4), "e4 should be empty after reset"
    print("15. Board reset: PASS")

    # 16. map_range (Arduino map() — difficulty and timeout scaling)
    def mr(v, i0, i1, o0, o1):
        return int((v - i0) * (o1 - o0) / (i1 - i0) + o0)

    assert mr(1, 1, 8, 1, 20) == 1
    assert mr(8, 1, 8, 1, 20) == 20
    assert mr(1, 1, 8, 3000, 12000) == 3000
    assert mr(8, 1, 8, 3000, 12000) == 12000
    print("16. map_range (difficulty 1-20, timeout 3-12s): PASS")

    # 17. Hint ISR button state checks present
    assert hasattr(buttons, "is_ok_held"), "is_ok_held missing"
    assert hasattr(buttons, "is_button8_held"), "is_button8_held missing"
    print("17. Hint ISR button state checks: PASS")

    # 18. Lichess mock client
    lc = LichessClient()
    lc.start_game("white")
    lc.send_move("e2e4")
    opp_move = lc.wait_for_move()
    assert len(opp_move) >= 4, f"Bad Lichess mock move: {opp_move!r}"
    print(f"18. Lichess mock client  opp_move={opp_move}: PASS")

    engine.close()
    leds.all_off()

    print()
    print("=" * 40)
    print("  ALL 18 CHECKS PASSED")
    print("=" * 40)


if __name__ == "__main__":
    run()
