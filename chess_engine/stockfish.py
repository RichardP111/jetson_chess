# =============================================================================
# chess_engine/stockfish.py
# Author : Richard Pu
# Created: 2026-06-10
# Purpose: Stockfish UCI wrapper via python-chess. Returns both the engine move
#          and a best-reply hint, mirroring the original Pi serial protocol.
#          Set STOCKFISH_PATH env var to override the default binary location.
# =============================================================================

import os
import logging
from typing import Optional, Tuple

import chess
import chess.engine

log = logging.getLogger(__name__)

STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")


class StockfishEngine:
    def __init__(self):
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
            log.info(f"Stockfish loaded: {STOCKFISH_PATH}")
        except FileNotFoundError:
            raise RuntimeError(
                f"Stockfish binary not found at '{STOCKFISH_PATH}'. "
                "Install: sudo apt install stockfish  "
                "or set STOCKFISH_PATH env var."
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def get_move(
        self,
        fen: str,
        skill_level: int = 10,
        movetime_ms: int = 5000,
    ) -> Tuple[str, str]:
        """
        Returns (engine_move_uci, suggested_best_next_uci).

        Mirrors the original Pi protocol:
          pisMove = 4-char UCI move + pisSuggestedBestMove
          pisSuggestedBestMove = pisMove.substring(5)  ← hint for the human

        suggested_best_next is the engine's best response *after* its own move
        (i.e. what the human should consider doing next). Empty string if the
        game is over (checkmate/stalemate).
        """
        board = chess.Board(fen)
        self._engine.configure({"Skill Level": max(0, min(20, skill_level))})
        limit = chess.engine.Limit(time=movetime_ms / 1000.0)

        result = self._engine.play(board, limit, info=chess.engine.INFO_ALL)
        engine_move = result.move.uci() if result.move else ""

        # Get best reply (hint for the human's next move)
        suggested = ""
        if result.move:
            board.push(result.move)
            if not board.is_game_over():
                hint_result = self._engine.play(
                    board,
                    chess.engine.Limit(time=0.1),
                )
                suggested = hint_result.move.uci() if hint_result.move else ""
            board.pop()

        log.info(f"Stockfish: move={engine_move}  hint={suggested}")
        return engine_move, suggested

    def is_legal(self, fen: str, uci: str) -> Optional[bool]:
        """
        Returns True/False if determinable, None on exception (caller retries).
        Mirrors checkPiForError() logic — a fast local check.
        Also tries with queen promotion appended for pawn promotion moves.
        """
        try:
            board = chess.Board(fen)
            move = chess.Move.from_uci(uci)
            if move in board.legal_moves:
                return True
            # Try queen promotion for 4-char pawn moves
            if len(uci) == 4:
                try:
                    promo = chess.Move.from_uci(uci + "q")
                    if promo in board.legal_moves:
                        return True
                except Exception:
                    pass
            return False
        except chess.InvalidMoveError:
            # Malformed UCI string (e.g. 'a1a1', null move) → illegal
            return False
        except Exception as e:
            log.warning(f"is_legal({uci}) exception: {e}")
            return None

    def close(self):
        try:
            self._engine.quit()
        except Exception:
            pass
