# =============================================================================
# chess_engine/stockfish.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: Stockfish UCI wrapper via python-chess. Returns both the engine
#          move and a best-reply hint, mirroring the original Pi serial
#          protocol
#          Set STOCKFISH_PATH env var to override the default binary location.
# =============================================================================

import logging
from typing import Optional, Tuple

import chess
import chess.engine

from config import CFG

log = logging.getLogger(__name__)


class StockfishEngine:
    def __init__(self):
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(CFG.stockfish_path)
            log.info(f"Stockfish loaded: {CFG.stockfish_path}")
        except FileNotFoundError:
            raise RuntimeError(
                f"Stockfish binary not found at '{CFG.stockfish_path}'. "
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

        suggested_best_next is the engine's best response *after* its own move
        (i.e. the hint for the human's next move).  Empty string if the game
        is over (checkmate / stalemate).
        """
        board = chess.Board(fen)
        self._engine.configure({"Skill Level": max(0, min(20, skill_level))})
        limit = chess.engine.Limit(time=movetime_ms / 1000.0)

        result = self._engine.play(board, limit, info=chess.engine.INFO_ALL)
        engine_move = result.move.uci() if result.move else ""

        suggested = ""
        if result.move:
            board.push(result.move)
            if not board.is_game_over():
                hint_result = self._engine.play(
                    board, chess.engine.Limit(time=0.1)
                )
                suggested = hint_result.move.uci() if hint_result.move else ""
            board.pop()

        log.info(f"Stockfish: move={engine_move}  hint={suggested}")
        return engine_move, suggested

    def evaluate(self, fen: str, time_s: float = 0.05) -> int:
        """
        Return the centipawn evaluation of the position from White's
        perspective.  Uses the already-open engine — no second process.
        Returns 0 on failure.
        """
        try:
            board = chess.Board(fen)
            info  = self._engine.analyse(board, chess.engine.Limit(time=time_s))
            score = info["score"].white().score(mate_score=10_000)
            return score if score is not None else 0
        except Exception as e:
            log.warning(f"evaluate() error: {e}")
            return 0

    def is_legal(self, fen: str, uci: str) -> Optional[bool]:
        """
        Returns True/False if determinable, None on exception (caller retries).
        Also tries queen promotion for bare pawn moves.
        """
        try:
            board = chess.Board(fen)
            move  = chess.Move.from_uci(uci)
            if move in board.legal_moves:
                return True
            if len(uci) == 4:
                try:
                    promo = chess.Move.from_uci(uci + "q")
                    if promo in board.legal_moves:
                        return True
                except Exception:
                    pass
            return False
        except chess.InvalidMoveError:
            return False
        except Exception as e:
            log.warning(f"is_legal({uci}) exception: {e}")
            return None

    def close(self):
        try:
            self._engine.quit()
        except Exception:
            pass
