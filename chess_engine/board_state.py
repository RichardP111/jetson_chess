from __future__ import annotations
# =============================================================================
# chess_engine/board_state.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: Dual-representation board state — mirrors the Arduino currentBoard
#          array for square occupancy, backed by a python-chess Board for FEN
#          generation, legal move checking, and move history.
#          New: undo support, capture detection, promotion detection.
# =============================================================================

import logging
import chess

from config import CFG

log = logging.getLogger(__name__)

COL_MAP = {"a": 0, "b": 1, "c": 2, "d": 3, "e": 4, "f": 5, "g": 6, "h": 7}


class BoardState:
    """
    Dual representation:
      - self.occupied[row][col]  : 1=piece present, 0=empty
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
                # Try with queen promotion suffix for bare pawn moves
                move = chess.Move.from_uci(uci + "q") if len(uci) == 4 else move
            self._chess_board.push(move)
            self._sync_occupied_from_chess()
            log.debug(f"Applied move {uci}. FEN: {self._chess_board.fen()}")
        except Exception as e:
            log.error(f"Failed to apply move {uci}: {e}")

    def undo_move(self) -> bool:
        """
        Pop the last move off the stack.
        Returns True if a move was undone, False if the stack was empty.
        """
        if not self._chess_board.move_stack:
            return False
        self._chess_board.pop()
        self._sync_occupied_from_chess()
        log.info("Move undone")
        return True

    def undo_half_moves(self, count: int) -> int:
        """
        Undo up to `count` half-moves. Returns how many were actually undone.
        Capped at CFG.undo_max_half_moves.
        """
        count = min(count, CFG.undo_max_half_moves, len(self._chess_board.move_stack))
        for _ in range(count):
            self._chess_board.pop()
        self._sync_occupied_from_chess()
        log.info(f"Undid {count} half-moves")
        return count

    def is_square_occupied(self, col: int, row: int) -> bool:
        """
        Fixed vertical rank inversion check.
        Input row: 0 = rank 1 (bottom), 7 = rank 8 (top).
        Internal occupied array row: 0 = rank 8 (top), 7 = rank 1 (bottom).
        """
        return self.occupied[7 - row][col] == 1

    def is_capture(self, uci: str) -> bool:
        """Return True if the move captures a piece (or is en passant)."""
        try:
            move  = chess.Move.from_uci(uci)
            board = self._chess_board
            return board.is_capture(move)
        except Exception:
            return False

    def is_promotion(self, uci: str) -> bool:
        """Return True if the move is a pawn reaching the back rank."""
        try:
            move  = chess.Move.from_uci(uci)
            piece = self._chess_board.piece_at(move.from_square)
            if piece is None or piece.piece_type != chess.PAWN:
                return False
            to_rank = chess.square_rank(move.to_square)
            return to_rank in (0, 7)
        except Exception:
            return False

    def promotion_move(self, uci: str, piece_char: str) -> str:
        """
        Return the full UCI string with the promotion piece appended.
        piece_char: 'q' | 'r' | 'b' | 'n'
        """
        base = uci[:4]
        return base + piece_char.lower()

    def col_from_char(self, c: str) -> int:
        return COL_MAP.get(c, 0)

    def row_from_char(self, c: str) -> int:
        """Convert rank char '1'-'8' to row index. rank 1 = 0 (bottom), rank 8 = 7 (top)."""
        return int(c) - 1

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

    def move_count(self) -> int:
        """Total half-moves played so far."""
        return len(self._chess_board.move_stack)

    # ── Private ───────────────────────────────────────────────────────────────

    def _sync_occupied_from_chess(self):
        """Rebuild the occupied[][] array from the python-chess board."""
        for row in range(8):
            for col in range(8):
                rank = 7 - row
                sq   = chess.square(col, rank)
                self.occupied[row][col] = 1 if self._chess_board.piece_at(sq) else 0

    def _move_history_uci(self) -> list:
        """Return all moves played so far as a list of UCI strings."""
        return [m.uci() for m in self._chess_board.move_stack]