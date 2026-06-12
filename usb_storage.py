# =============================================================================
# usb_storage.py
# Author : Richard Pu
# Created: 2026-06-12
# Purpose: Optional USB-based game save / load.
#          If a USB drive is plugged in, games are saved as PGN files.
#          If no USB drive is present the feature is completely silent —
#          no errors, no prompts.
#
# Usage:
#   from usb_storage import usb
#   usb.save_game(board, game_mode, difficulty)   # saves if USB present
#   games = usb.list_games()                       # [] if no USB
#   pgn_text = usb.load_game(filename)            # None if not found
# =============================================================================

import os
import glob
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import chess
import chess.pgn

from config import CFG

log = logging.getLogger(__name__)


def _find_usb_root() -> Optional[Path]:
    """
    Return the mount point of the first removable USB storage device found,
    or None if nothing is plugged in.
    Checks common Linux mount parent directories from CFG.
    """
    for base in CFG.usb_mount_paths:
        base_path = Path(base)
        if not base_path.exists():
            continue
        # Each sub-directory directly under base is a potential mount point
        for candidate in sorted(base_path.iterdir()):
            if not candidate.is_dir():
                continue
            # Skip system-looking names
            if candidate.name.startswith("."):
                continue
            # A real mount has something in it and isn't the base itself
            try:
                contents = list(candidate.iterdir())
                if contents:
                    return candidate
            except PermissionError:
                continue
    return None


def _ensure_game_dir(usb_root: Path) -> Path:
    game_dir = usb_root / CFG.pgn_subdir
    game_dir.mkdir(exist_ok=True)
    return game_dir


class USBStorage:
    """
    Thin wrapper around PGN file I/O on a USB drive.
    All public methods are safe to call even when no USB drive is present.
    """

    def is_available(self) -> bool:
        """True if a USB drive is currently mounted and writable."""
        root = _find_usb_root()
        if root is None:
            return False
        return os.access(root, os.W_OK)

    def save_game(
        self,
        move_history_uci: list,
        game_mode: str,
        difficulty: int = 0,
        result: str = "*",
    ) -> Optional[str]:
        """
        Write a PGN file to the USB drive.
        Returns the filename written, or None if no USB is present.

        move_history_uci : list of UCI strings in move order
        game_mode        : 'Stockfish' | 'OnlineHuman' | 'LocalHuman'
        difficulty       : Stockfish skill level (0 = N/A)
        result           : PGN result string e.g. '1-0', '0-1', '1/2-1/2', '*'
        """
        root = _find_usb_root()
        if root is None:
            log.debug("USB save skipped — no drive mounted")
            return None

        try:
            game_dir = _ensure_game_dir(root)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            filename  = f"chess_{timestamp}.pgn"
            filepath  = game_dir / filename

            game = chess.pgn.Game()
            game.headers["Event"]  = "Jetson Smart Chess Board"
            game.headers["Site"]   = "Local"
            game.headers["Date"]   = datetime.now().strftime("%Y.%m.%d")
            game.headers["White"]  = "Human"
            game.headers["Black"]  = game_mode
            game.headers["Result"] = result
            if difficulty:
                game.headers["WhiteElo"] = "?"
                game.headers["BlackElo"] = str(difficulty * 100)

            node = game
            board = chess.Board()
            for uci in move_history_uci:
                try:
                    move = chess.Move.from_uci(uci)
                    node = node.add_variation(move)
                    board.push(move)
                except Exception:
                    pass

            with open(filepath, "w") as f:
                exporter = chess.pgn.FileExporter(f)
                game.accept(exporter)

            log.info(f"Game saved: {filepath}")
            return filename

        except Exception as e:
            log.error(f"Failed to save game: {e}")
            return None

    def list_games(self) -> list:
        """
        Return a list of PGN filenames on the USB drive, newest first.
        Returns [] if no USB is present.
        """
        root = _find_usb_root()
        if root is None:
            return []
        game_dir = root / CFG.pgn_subdir
        if not game_dir.exists():
            return []
        files = sorted(game_dir.glob("*.pgn"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [f.name for f in files]

    def load_game(self, filename: str) -> Optional[str]:
        """
        Return the raw PGN text for a saved game, or None if not found.
        """
        root = _find_usb_root()
        if root is None:
            return None
        path = root / CFG.pgn_subdir / filename
        if not path.exists():
            return None
        try:
            return path.read_text()
        except Exception as e:
            log.error(f"Failed to load {filename}: {e}")
            return None

    def delete_game(self, filename: str) -> bool:
        """Delete a saved game file. Returns True on success."""
        root = _find_usb_root()
        if root is None:
            return False
        path = root / CFG.pgn_subdir / filename
        try:
            path.unlink()
            return True
        except Exception:
            return False


# Singleton used across the project
usb = USBStorage()
