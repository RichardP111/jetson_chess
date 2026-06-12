# =============================================================================
# tests/test_mock_game.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: Full mock integration test — pytest fixtures, covers all features
#          including new: promotion, undo, local mode, USB storage mock,
#          voice mock, analysis, eval, custom themes.
#          No hardware, Lichess token, or Stockfish required.
# Run:    pytest tests/test_mock_game.py -v
# =============================================================================

import os
import sys
import builtins
import unittest.mock as mock

os.environ["MOCK_LEDS"]    = "1"
os.environ["MOCK_BUTTONS"] = "1"
os.environ["MOCK_ONLINE"]  = "1"
os.environ["MOCK_OLED"]    = "1"
os.environ["MOCK_VOICE"]   = "1"
os.environ["FLASK_SECRET_KEY"] = "test-secret-key-for-testing-only"

# Prevent mock keyboard thread from blocking on stdin during test
builtins.input = lambda *a, **k: (_ for _ in ()).throw(EOFError())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
import pytest

logging.basicConfig(level=logging.WARNING)

from hardware.leds    import LEDController
from hardware.buttons import ButtonController
from chess_engine.board_state import BoardState
from chess_engine.stockfish   import StockfishEngine
from ui.display   import Display
from ui.animations import AnimationEngine
from oled.oled_display import OLEDDisplay
from online.lichess import LichessClient
from voice import voice
from usb_storage import usb


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def leds():
    return LEDController()

@pytest.fixture(scope="module")
def buttons():
    return ButtonController()

@pytest.fixture(scope="module")
def board():
    return BoardState()

@pytest.fixture(scope="module")
def engine():
    e = StockfishEngine()
    yield e
    e.close()

@pytest.fixture(scope="module")
def display(leds, board, buttons):
    return Display(leds, board, buttons)

@pytest.fixture(scope="module")
def anim(leds, board):
    return AnimationEngine(leds, board)

@pytest.fixture(scope="module")
def oled():
    return OLEDDisplay()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_coordinate_parsing(board):
    assert board.parse_uci("e2e4") == (4, 6, 4, 4)
    assert board.parse_uci("a1h8") == (0, 7, 7, 0)
    assert board.parse_uci("h8a1") == (7, 0, 0, 7)


def test_starting_position_occupied(board):
    board.reset()
    for row in [0, 1, 6, 7]:
        for col in range(8):
            assert board.is_square_occupied(col, row)
    for row in range(2, 6):
        for col in range(8):
            assert not board.is_square_occupied(col, row)


def test_legal_move_check(board, engine):
    board.reset()
    assert engine.is_legal(board.fen(), "e2e4") is True
    assert engine.is_legal(board.fen(), "e2e5") is False
    assert engine.is_legal(board.fen(), "g1f3") is True
    assert engine.is_legal(board.fen(), "a1a1") is False


def test_move_application(board):
    board.reset()
    for m in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]:
        board.apply_move(m)
    assert board.is_square_occupied(4, 4)       # e4 occupied
    assert not board.is_square_occupied(4, 6)   # e2 empty


def test_undo(board):
    board.reset()
    board.apply_move("e2e4")
    board.apply_move("e7e5")
    assert board.is_square_occupied(4, 4)   # e4
    board.undo_move()
    assert not board.is_square_occupied(4, 3)   # e5 undone
    assert board.is_square_occupied(4, 1)        # e7 restored
    board.undo_half_moves(1)
    assert not board.is_square_occupied(4, 4)    # e4 undone


def test_capture_detection(board):
    board.reset()
    for m in ["e2e4", "d7d5"]:
        board.apply_move(m)
    assert board.is_capture("e4d5")
    assert not board.is_capture("d1d2")


def test_promotion_detection(board):
    # Manually push a board close to promotion
    import chess
    b = chess.Board("8/P7/8/8/8/8/8/8 w - - 0 1")
    bs = BoardState()
    bs._chess_board = b
    bs._sync_occupied_from_chess()
    assert bs.is_promotion("a7a8")
    assert bs.promotion_move("a7a8", "q") == "a7a8q"
    assert bs.promotion_move("a7a8", "r") == "a7a8r"


def test_light_up_move_y(display):
    display.light_up_move("e2e4", mode="Y")
    display.light_up_move("g1f3", mode="Y")


def test_light_up_move_n(display, buttons):
    with mock.patch.object(buttons, "detect_button", return_value=9):
        display.light_up_move("f1c4", mode="N")


def test_light_up_move_h(display):
    with mock.patch("time.sleep"):
        display.light_up_move("c4f7", mode="H")


def test_capture_flash(display, board):
    board.reset()
    for m in ["e2e4", "d7d5", "e4d5"]:
        board.apply_move(m)
    display.light_up_move("e4d5", mode="Y")


def test_board_markings(display):
    display.show_board_markings()
    display.show_opening_markings()


def test_setup_icons(display):
    display.show_difficulty_icon()
    display.show_timeout_icon()
    display.show_colour_choice_icon()
    display.show_undo_icon()


def test_promotion_display(display):
    display.show_promotion_choices()


def test_error_animation(display):
    with mock.patch("time.sleep"):
        display.error_animation()


def test_checkmate_animation(display):
    with mock.patch("time.sleep"):
        display.checkmate_animation("e7e8")


def test_loading_animation(display):
    with mock.patch("time.sleep"):
        display.loading_animation_fast()


def test_engine_get_move(board, engine):
    board.reset()
    for m in ["e2e4", "e7e5", "g1f3"]:
        board.apply_move(m)
    move, hint = engine.get_move(board.fen(), skill_level=5, movetime_ms=500)
    assert len(move) >= 4


def test_engine_evaluate(board, engine):
    board.reset()
    score = engine.evaluate(board.fen())
    assert isinstance(score, int)


def test_board_reset(board):
    board.reset()
    assert board.is_square_occupied(4, 6)
    assert not board.is_square_occupied(4, 4)


def test_map_range():
    def mr(v, i0, i1, o0, o1):
        return int((v - i0) * (o1 - o0) / (i1 - i0) + o0)
    assert mr(1, 1, 8, 1, 20) == 1
    assert mr(8, 1, 8, 1, 20) == 20
    assert mr(1, 1, 8, 3000, 12000) == 3000
    assert mr(8, 1, 8, 3000, 12000) == 12000


def test_button_state_checks(buttons):
    assert hasattr(buttons, "is_ok_held")
    assert hasattr(buttons, "is_button8_held")


def test_lichess_mock_client():
    lc = LichessClient()
    lc.start_game("white")
    lc.send_move("e2e4")
    opp = lc.wait_for_move()
    assert len(opp) >= 4


def test_voice_mock():
    assert voice._backend == "mock"
    voice.say("Test phrase")
    voice.announce_move("e2e4", captured=False)
    voice.announce_move("e4d5", captured=True)
    voice.announce_move("e7e8q", promotion="queen")
    voice.announce_status("Check!")


def test_usb_storage_list_no_drive():
    # Without a real USB, list_games should return []
    with mock.patch("usb_storage._find_usb_root", return_value=None):
        result = usb.list_games()
        assert result == []


def test_usb_storage_save_no_drive(board):
    board.reset()
    with mock.patch("usb_storage._find_usb_root", return_value=None):
        result = usb.save_game([], "Stockfish")
        assert result is None


def test_custom_theme(anim):
    anim.set_theme("neon")
    assert anim._theme_name == "neon"
    anim.set_theme({"light": "#ffffff", "dark": "#000000",
                    "move": "#00ff00", "hint": "#00ffff"})
    assert anim._theme_name == "custom"
    # Reset
    anim.set_theme("classic")


def test_oled_screens(oled):
    oled.show_mode_select()
    oled.show_setup_difficulty(5)
    oled.show_setup_timeout(5000)
    oled.show_setup_colour()
    oled.show_game("White", status="Test", eval_score=120)
    oled.show_hint("e2e4")
    oled.show_promotion_select("White")
    oled.show_undo_confirm(2)
    oled.show_usb_saved("chess_2026.pgn")
    oled.show_analysis([{"move": "e4d5", "loss": 320, "best": "g1f3"}])
    oled.show_checkmate("White")
    oled.show_new_game()
    oled.show_local_game_start()
    oled.show_loading(32, 64)


def test_animations(anim, board):
    board.reset()
    anim.show_board_themed()
    with mock.patch("time.sleep"):
        anim.board_wipe((0, 0, 0))
        anim.undo_sweep()
        anim.promotion_flash("q")
        anim.promotion_flash("r")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
