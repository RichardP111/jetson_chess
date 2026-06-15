# =============================================================================
# online/lichess.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-15
# =============================================================================

import os
import time
import queue
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
        log.warning("berserk not found — online play disabled. "
                    "Install with: pip install berserk")
        MOCK_ONLINE = True


class LichessClient:
    def __init__(self):
        self._game_id:    Optional[str] = None
        self._colour:     Optional[str] = None
        self._client:     Any = None
        self._board_client: Any = None

        # Queue instead of single slot — never drops moves
        self._move_queue: queue.Queue = queue.Queue()

        # Track how many half-moves we've already seen so we don't re-fire
        # on the same gameState event that echoes back our own move
        self._seen_move_count: int = 0

        if MOCK_ONLINE:
            log.warning("LichessClient running in MOCK mode — no real games.")
            return

        session             = berserk.TokenSession(LICHESS_TOKEN)
        self._client        = berserk.Client(session=session)
        self._board_client  = berserk.clients.Board(session=session)
        log.info("LichessClient connected")

    # ── Public API ─────────────────────────────────────────────────────────

    def start_game(self, colour: str = "white", opponent: str = "human"):
        """
        Start a Lichess game.

        opponent:
          "human"   — open seek for a rated human opponent (unrated, 10+0)
          "ai_easy" — Stockfish level 1
          "ai_hard" — Stockfish level 8
        """
        self._colour = colour
        # Clear stale moves from any previous game
        while not self._move_queue.empty():
            self._move_queue.get_nowait()
        self._seen_move_count = 0

        if MOCK_ONLINE:
            self._game_id = "mock_game_001"
            log.info(f"[MOCK] Lichess game started as {colour} vs {opponent}")
            return

        color: Literal["white", "black"] = "black" if colour == "black" else "white"
        log.info(f"Starting Lichess game as {colour} vs {opponent}...")

        try:
            if opponent == "human":
                # Create an open seek — anyone can accept
                # berserk's create_seek blocks until someone accepts
                game = self._client.board.seek(
                    time=10,
                    increment=0,
                    rated=False,
                    color=color,
                )
                # seek returns the game object once accepted
                self._game_id = game.get("id") or game.get("gameId")
            else:
                # AI game
                ai_level = 8 if opponent == "ai_hard" else 1
                game = self._client.challenges.create_ai(
                    level=ai_level,
                    clock_limit=600,
                    clock_increment=0,
                    color=color,
                )
                self._game_id = game["id"]

            log.info(f"Lichess game started: {self._game_id}")
            self._start_stream_thread()

        except Exception as e:
            log.error(f"Failed to start Lichess game: {e}")
            raise

    def send_move(self, uci: str):
        """Send the human's move to Lichess."""
        if MOCK_ONLINE:
            log.info(f"[MOCK] Sent move: {uci}")
            return
        try:
            if not self._board_client or not self._game_id:
                raise RuntimeError("Lichess not initialised")
            self._board_client.make_move(self._game_id, uci)
            log.info(f"Move sent: {uci}")
        except Exception as e:
            log.error(f"Failed to send move {uci}: {e}")
            raise

    def wait_for_move(self, timeout: float = 300.0) -> str:
        """Block until the opponent's next move arrives. Returns UCI string."""
        if MOCK_ONLINE:
            log.info("[MOCK] Waiting for opponent move...")
            time.sleep(2)
            return "e7e5"

        try:
            move = self._move_queue.get(timeout=timeout)
            log.info(f"Opponent move dequeued: {move}")
            return move
        except queue.Empty:
            log.warning("wait_for_move timed out")
            return ""

    # ── Stream ─────────────────────────────────────────────────────────────

    def _start_stream_thread(self):
        t = threading.Thread(target=self._stream_game, daemon=True,
                             name="lichess-stream")
        t.start()

    def _stream_game(self):
        if not self._game_id or MOCK_ONLINE:
            return
        log.info(f"Starting stream for game {self._game_id}")
        try:
            for event in self._board_client.stream_game_state(self._game_id):
                etype = event.get("type")

                if etype == "gameFull":
                    # Initial snapshot — contains full state including any
                    # moves already played (e.g. if we're Black and White
                    # already moved before the stream connected)
                    state = event.get("state", {})
                    self._process_state(state)

                elif etype == "gameState":
                    self._process_state(event)

                elif etype == "gameFinish":
                    log.info("Lichess game finished")
                    # Unblock any waiting wait_for_move
                    self._move_queue.put("")
                    break

                elif etype == "chatLine":
                    pass  # ignore chat

                else:
                    log.debug(f"Unhandled event type: {etype}")

        except Exception as e:
            log.error(f"Lichess stream error: {e}")
            # Unblock wait_for_move so the game can handle the disconnect
            self._move_queue.put("")

    def _process_state(self, state: dict):
        """
        Parse a gameState dict and enqueue the opponent's move if it's new.

        Lichess sends the full move list as a space-separated string.
        e.g. after 3 half-moves: "e2e4 e7e5 g1f3"

        We're White  → opponent moves are at indices 1, 3, 5 ... (even count = our turn)
        We're Black  → opponent moves are at indices 0, 2, 4 ... (odd count  = our turn)

        We track _seen_move_count so we only fire once per new opponent move.
        """
        moves_str = state.get("moves", "").strip()
        moves = moves_str.split() if moves_str else []
        total = len(moves)

        log.debug(f"_process_state: total={total} seen={self._seen_move_count} "
                  f"colour={self._colour}")

        if total <= self._seen_move_count:
            # No new moves since last check
            return

        # Work out whose turn it is AFTER all moves so far:
        # After 0 moves → White to move
        # After 1 move  → Black to move
        # After 2 moves → White to move   (etc.)
        # So: White's moves are at even indices (0, 2, 4...)
        #     Black's moves are at odd  indices (1, 3, 5...)

        # Check each new move since we last looked
        for idx in range(self._seen_move_count, total):
            move = moves[idx]
            # Even index = White's move, odd index = Black's move
            is_white_move = (idx % 2 == 0)
            we_are_white  = (self._colour == "white")

            is_our_move = (is_white_move == we_are_white)

            if not is_our_move:
                # This is the opponent's move — enqueue it
                log.info(f"Opponent move at idx {idx}: {move}")
                self._move_queue.put(move)

        self._seen_move_count = total