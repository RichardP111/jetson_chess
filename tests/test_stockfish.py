# =============================================================================
# tests/test_stockfish.py
# Author : Richard Pu
# Created: 2026-06-10
# Purpose: Smoke test confirming Stockfish is installed, responding to UCI,
#          and returning legal moves and eval hints for a test position.
# =============================================================================

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chess_engine.stockfish import StockfishEngine
from chess_engine.board_state import BoardState


def main():
    print("Testing Stockfish engine...")
    board = BoardState()
    engine = StockfishEngine()

    print(f"Starting FEN: {board.fen()}")

    # Test a few moves
    test_moves = ["e2e4", "e7e5", "g1f3"]
    for move in test_moves:
        legal = engine.is_legal(board.fen(), move)
        print(f"  is_legal({move}) = {legal}")
        if legal:
            board.apply_move(move)

    print(f"\nCurrent FEN: {board.fen()}")

    print("\nAsking Stockfish for best move (skill=5, 1s)...")
    engine_move, hint = engine.get_move(board.fen(), skill_level=5, movetime_ms=1000)
    print(f"  Engine move: {engine_move}")
    print(f"  Suggested hint: {hint}")

    engine.close()
    print("\nStockfish test passed!")


if __name__ == "__main__":
    main()
