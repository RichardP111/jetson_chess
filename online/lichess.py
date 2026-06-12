# =============================================================================
# online/lichess.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: Lichess board API integration via berserk. Handles game creation,
#          move submission, and opponent move streaming. Falls back to mock
#          mode if LICHESS_TOKEN is unset or MOCK_ONLINE=1 is set.
# =============================================================================

import os
import time
import logging
import threading
from typing import Any, Literal, Optional

log = logging.getLogger(__name__)

LICHESS_TOKEN = os.environ.get("LICHESS_TOKEN", "")
MOCK_ONLINE   = os.environ.get("MOCK_ONLINE", "0") == "1" or not LICHESS_TOKEN

if not MOCK_ONLINE:
    try:
        import berserk
    except ImportError:
        log.warning(
            "berserk not found — online play disabled. "
            "Install with: pip install berserk --break-system-packages"
        )
        MOCK_ONLINE = True


class LichessClient:
    def __init__(self):
        self._game_id: Optional[str] = None
        self._colour: Optional[str]  = None
        self._pending_move: Optional[str] = None
        self._move_event = threading.Event()
        self._client: Any = None
        self._board_client: Any = None

        if MOCK_ONLINE:
            log.warning("LichessClient running in MOCK mode — no real games.")
            return

        session            = berserk.TokenSession(LICHESS_TOKEN)
        self._client       = berserk.Client(session=session)
        self._board_client = berserk.clients.Board(session=session)
        log.info("LichessClient connected")

    # ── Public API ────────────────────────────────────────────────────────────

    def start_game(self, colour: str = "white"):
        """
        Challenge the Lichess AI (level 1) or seek a human game.
        Modify clock_limit / clock_increment for different time controls.
        """
        self._colour = colour

        if MOCK_ONLINE:
            self._game_id = "mock_game_001"
            log.info(f"[MOCK] Lichess game started as {colour}")
            return

        log.info(f"Seeking Lichess game as {colour}...")
        try:
            color: Literal["white", "black"] = "black" if colour == "black" else "white"
            game = self._client.challenges.create_ai(
                level=1,
                clock_limit=600,
                clock_increment=0,
                color=color,
            )
            self._game_id = game["id"]
            log.info(f"Lichess game started: {self._game_id}")
            self._start_stream_thread()
        except Exception as e:
            log.error(f"Failed to start Lichess game: {e}")

    def send_move(self, uci: str):
        """Send the human's move to Lichess."""
        if MOCK_ONLINE:
            log.info(f"[MOCK] Sent move to Lichess: {uci}")
            return
        try:
            board_client = self._board_client
            if board_client is None:
                raise RuntimeError("Lichess board client is not initialized")
            game_id = self._game_id
            if game_id is None:
                raise RuntimeError("Lichess game is not initialized")
            board_client.make_move(game_id, uci)
            log.info(f"Move sent to Lichess: {uci}")
        except Exception as e:
            log.error(f"Failed to send move {uci}: {e}")

    def wait_for_move(self, timeout: float = 300.0) -> str:
        """Block until opponent's move arrives. Returns UCI string."""
        if MOCK_ONLINE:
            log.info("[MOCK] Waiting for opponent move (returning e7e5 after 2s)...")
            time.sleep(2)
            return "e7e5"

        self._move_event.clear()
        self._move_event.wait(timeout=timeout)
        move = self._pending_move or ""
        self._pending_move = None
        return move

    # ── Internal stream ───────────────────────────────────────────────────────

    def _start_stream_thread(self):
        t = threading.Thread(target=self._stream_game, daemon=True)
        t.start()

    def _stream_game(self):
        if not self._game_id or MOCK_ONLINE:
            return
        try:
            board_client = self._board_client
            if board_client is None:
                raise RuntimeError("Lichess board client is not initialized")
            game_id = self._game_id
            if game_id is None:
                raise RuntimeError("Lichess game is not initialized")
            for event in board_client.stream_game_state(game_id):
                etype = event.get("type")
                if etype == "gameState":
                    moves = event.get("moves", "").split()
                    if moves:
                        last_move        = moves[-1]
                        our_turn_index   = 0 if self._colour == "white" else 1
                        if len(moves) % 2 != our_turn_index:
                            self._pending_move = last_move
                            self._move_event.set()
                            log.info(f"Opponent move received: {last_move}")
                elif etype == "gameFinish":
                    log.info("Lichess game finished")
                    break
        except Exception as e:
            log.error(f"Lichess stream error: {e}")
