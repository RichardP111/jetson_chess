# =============================================================================
# usb_storage.py
# Author : Richard Pu
# Created: 2026-06-12
# Purpose: Optional USB-based game save / load.
#          If a USB drive is plugged in, games are saved as PGN files.
#          If no USB drive is present the feature is completely silent —
#          no errors, no prompts.
# =============================================================================

import os
import logging
import chess.pgn
from datetime import datetime
from config import CFG

log = logging.getLogger("usb_storage")

class USBStorage:
    def __init__(self):
        self.mount_base_paths = CFG.usb_mount_paths
        self.subdir = CFG.pgn_subdir

    def get_active_mount(self) -> str | None:
        """
        Checks our dedicated universal mount point first, then falls back 
        to checking standard system paths if needed.
        """
        # 1. Test our universal hardwired path first
        universal_path = "/mnt/usb"
        if os.path.exists(universal_path) and os.path.ismount(universal_path):
            try:
                # Double-check that we can actually list contents (it's not a ghost mount)
                os.listdir(universal_path)
                return universal_path
            except Exception:
                pass # Mount point exists but drive might have been yanked out

        # 2. Fallback to scanning system directories if the universal mount isn't active
        for base in self.mount_base_paths:
            if not os.path.exists(base):
                continue
            for item in os.listdir(base):
                full_path = os.path.join(base, item)
                if os.path.isdir(full_path) and not item.startswith('.'):
                    if item not in ("etc", "usr", "var", "bin", "sbin", "lib", "proc", "sys", "dev"):
                        return full_path
        return None

    def is_available(self) -> bool:
        """
        Returns True only if a drive is physically plugged in and actively mounted.
        """
        mount = self.get_active_mount()
        if not mount:
            return False
            
        # Ensure we can actually write to the disk (checks if drive is write-protected or pulled)
        return os.access(mount, os.W_OK)

    def save_game(self, move_history_uci: list, game_mode: str, difficulty: int) -> str | None:
        mount = self.get_active_mount()
        if not mount:
            return None

        target_dir = os.path.join(mount, self.subdir)
        os.makedirs(target_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"game_{timestamp}_{game_mode.lower()}.pgn"
        full_path = os.path.join(target_dir, filename)

        try:
            game = chess.pgn.Game()
            game.headers["Event"] = f"Jetson Chess Board Match"
            game.headers["Date"] = datetime.now().strftime("%Y.%m.%d")
            game.headers["Round"] = "1"
            game.headers["White"] = "Human" if game_mode != "OnlineHuman" else "LocalPlayer"
            game.headers["Black"] = f"Stockfish (LVL {difficulty})" if game_mode == "Stockfish" else "Opponent"
            game.headers["Result"] = "*"

            node = game
            for uci in move_history_uci:
                move = chess.Move.from_uci(uci)
                node = node.add_variation(move)

            with open(full_path, "w", encoding="utf-8") as f:
                exporter = chess.pgn.FileExporter(f)
                game.accept(exporter)

            log.info(f"Successfully saved game to USB: {full_path}")
            return filename
        except Exception as e:
            log.error(f"Failed to save game to USB: {e}")
            return None

    # ── NEW: LIST SAVED GAMES FROM USB ──
    def list_saved_games(self) -> list:
        """
        Returns a list of all .pgn game files found on the USB drive.
        """
        mount = self.get_active_mount()
        if not mount:
            return []
        
        target_dir = os.path.join(mount, self.subdir)
        if not os.path.exists(target_dir):
            return []

        try:
            files = [f for f in os.listdir(target_dir) if f.endswith(".pgn")]
            files.sort(reverse=True)  # Newest games first
            return files
        except Exception as e:
            log.error(f"Error reading directory {target_dir}: {e}")
            return []

    # ── NEW: LOAD SPECIFIC GAME FROM USB ──
    def load_game(self, filename: str) -> list | None:
        """
        Reads a PGN file from the USB and returns the move history as a list of UCI strings.
        """
        mount = self.get_active_mount()
        if not mount:
            return None

        full_path = os.path.join(mount, self.subdir, filename)
        if not os.path.exists(full_path):
            log.error(f"File not found: {full_path}")
            return None

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                game = chess.pgn.read_game(f)
                if not game:
                    return None
                
                # Convert the internal PGN move tree structure down to a clean UCI string list
                uci_moves = [move.uci() for move in game.mainline_moves()]
                return uci_moves
        except Exception as e:
            log.error(f"Failed to parse PGN file {filename}: {e}")
            return None

usb = USBStorage()