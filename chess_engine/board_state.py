# =============================================================================
# chess_engine/board_state.py
# Author : Richard Pu
# Created: 2026-06-10
# Purpose: Dual-representation board state — mirrors the Arduino currentBoard
#          array for square occupancy, backed by a python-chess Board for FEN
#          generation, legal move checking, and move history.
# =============================================================================

import logging
import chess

log = logging.getLogger(__name__)

COL_MAP = {"a": 0, "b": 1, "c": 2, "d": 3, "e": 4, "f": 5, "g": 6, "h": 7}


class BoardState:
    """
    Dual representation:
      - self.occupied[row][col]  : 1=piece present, 0=empty  (like Arduino array)
      - self._chess_board        : python-chess Board for FEN / legal moves
    Row 0 = rank 8 (top of board), row 7 = rank 1 (bottom).
    """

    def __init__(self):
        self._chess_board = chess.Board()
        self.occupied: list[list[int]] = [[0] * 8 for _ in range(8)]
        self._sync_occupied_from_chess()
        log.info("BoardState initialised")

    # ── Public API ────────────────────────────────────────────────────────────

    def reset(self):
        self._chess_board.reset()
        self._sync_occupied_from_chess()
        log.info("Board reset to starting position")

    def fen(self) -> str:
        return self._chess_board.fen()

    def apply_move(self, uci: str):
        """
        Apply a UCI move string (e.g. 'e2e4') to both representations.
        Handles castling, en passant, and promotion automatically via python-chess.
        """
        try:
            move = chess.Move.from_uci(uci)
            if move not in self._chess_board.legal_moves:
                # Try with queen promotion suffix for pawn moves
                move = chess.Move.from_uci(uci + "q") if len(uci) == 4 else move
            self._chess_board.push(move)
            self._sync_occupied_from_chess()
            log.debug(f"Applied move {uci}. FEN: {self._chess_board.fen()}")
        except Exception as e:
            log.error(f"Failed to apply move {uci}: {e}")

    def is_square_occupied(self, col: int, row: int) -> bool:
        """row 0 = rank 8, col 0 = file a."""
        return self.occupied[row][col] == 1

    def col_from_char(self, c: str) -> int:
        return COL_MAP.get(c, 0)

    def row_from_char(self, c: str) -> int:
        """Convert rank char '1'-'8' to internal row index (0=rank8, 7=rank1)."""
        return 7 - (int(c) - 1)

    def parse_uci(self, uci: str):
        """Return (from_col, from_row, to_col, to_row) for a UCI string."""
        fc = self.col_from_char(uci[0])
        fr = self.row_from_char(uci[1])
        tc = self.col_from_char(uci[2])
        tr = self.row_from_char(uci[3])
        return fc, fr, tc, tr

    def print_board(self):
        log.info("Board state:")
        for r in range(8):
            log.info(" ".join(str(self.occupied[r][c]) for c in range(8)))

    # ── Private ───────────────────────────────────────────────────────────────

    def _sync_occupied_from_chess(self):
        """Rebuild the occupied[][] array from the python-chess board."""
        for row in range(8):
            for col in range(8):
                # python-chess square: rank 0..7 bottom-to-top, file 0..7 a..h
                rank = 7 - row  # our row 0 = rank 7 (chess rank 8)
                sq = chess.square(col, rank)
                self.occupied[row][col] = 1 if self._chess_board.piece_at(sq) else 0

    def _move_history_uci(self) -> list:
        """Return all moves played so far as a list of UCI strings."""
        return [m.uci() for m in self._chess_board.move_stack]
