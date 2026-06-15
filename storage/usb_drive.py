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

log = logging.getLogger("storage.usb_drive")

class USBStorage:
    def __init__(self):
        self.mount_base_paths = CFG.usb_mount_paths
        self.subdir = CFG.pgn_subdir

    MOUNT_POINT = "/mnt/usb"
    MOUNT_SCRIPT = "/usr/local/bin/chess-usb-mount.sh"

    def _find_usb_devices(self) -> list[str]:
        """
        Return all USB block device nodes using lsblk JSON.
        Checks both with and without fstype field since lsblk behaviour
        differs between kernels when a device is already mounted.
        """
        import subprocess, json
        devices = []
        try:
            r = subprocess.run(
                ["lsblk", "-o", "NAME,TRAN,FSTYPE,TYPE", "-J"],
                capture_output=True, text=True, timeout=5,
            )
            data = json.loads(r.stdout)
            for dev in data.get("blockdevices", []):
                if dev.get("tran") == "usb":
                    # Add partition children first (preferred)
                    for child in dev.get("children", []):
                        if child.get("type") in ("part", "disk"):
                            devices.append("/dev/" + child["name"])
                    # Also add whole disk as fallback
                    devices.append("/dev/" + dev["name"])
        except Exception as e:
            log.debug(f"_find_usb_devices lsblk: {e}")
        return devices

    def _mounted_at(self) -> str | None:
        """
        Scan /proc/mounts for any of our USB device nodes.
        This works regardless of whether lsblk reports fstype or not,
        and regardless of which mount point the OS chose.
        """
        usb_devs = set(self._find_usb_devices())

        # Also always include /dev/sda* as candidates — on this Jetson
        # USB storage always appears as /dev/sda regardless of port
        import glob
        for node in glob.glob("/dev/sd*"):
            usb_devs.add(node)

        if not usb_devs:
            return None

        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] in usb_devs:
                        return parts[1]
        except Exception as e:
            log.debug(f"_mounted_at /proc/mounts: {e}")
        return None

    def _try_software_mount(self) -> bool:
        """
        Mount the USB device. Tries three methods in order:
          1. Direct mount (if root)
          2. sudo mount (if sudoers rule allows it)
          3. The install script via sudo
        """
        import subprocess

        if os.path.ismount(self.MOUNT_POINT):
            return True

        devs = self._find_usb_devices()
        dev = next((d for d in devs if d[-1].isdigit()), next(iter(devs), None))
        if not dev:
            log.debug("_try_software_mount: no USB block device found")
            return False

        os.makedirs(self.MOUNT_POINT, exist_ok=True)

        # Detect filesystem type
        try:
            fs = subprocess.run(
                ["/sbin/blkid", "-o", "value", "-s", "TYPE", dev],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or "vfat"
        except Exception:
            fs = "vfat"

        # Build mount command
        if fs in ("vfat", "fat32", "fat16"):
            mount_args = ["-t", "vfat", "-o", "rw,uid=0,gid=0,umask=000,flush"]
        elif fs == "exfat":
            mount_args = ["-t", "exfat", "-o", "rw,uid=0,gid=0,umask=000"]
        elif fs == "ntfs":
            mount_args = ["-t", "ntfs", "-o", "rw"]
        else:
            mount_args = ["-o", "rw"]

        # Method 1: direct mount (running as root)
        if os.geteuid() == 0:
            r = subprocess.run(
                ["/bin/mount"] + mount_args + [dev, self.MOUNT_POINT],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0 and os.path.ismount(self.MOUNT_POINT):
                log.info(f"Mounted {dev} ({fs}) at {self.MOUNT_POINT}")
                return True
            log.warning(f"Direct mount failed rc={r.returncode}: {r.stderr.decode().strip()}")

        # Method 2: sudo mount
        r = subprocess.run(
            ["sudo", "-n", "/bin/mount"] + mount_args + [dev, self.MOUNT_POINT],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0 and os.path.ismount(self.MOUNT_POINT):
            log.info(f"sudo-mounted {dev} ({fs}) at {self.MOUNT_POINT}")
            return True

        # Method 3: sudo mount script
        if os.path.exists(self.MOUNT_SCRIPT):
            r = subprocess.run(
                ["sudo", "-n", self.MOUNT_SCRIPT, "mount", dev],
                capture_output=True, timeout=15,
            )
            if r.returncode == 0 and os.path.ismount(self.MOUNT_POINT):
                log.info(f"Script-mounted {dev} at {self.MOUNT_POINT}")
                return True

        log.warning(f"All mount attempts failed for {dev}")
        return False

    def get_active_mount(self) -> str | None:
        """
        Returns the path where a USB drive is mounted, or None.
        1. /mnt/usb already mounted
        2. USB device found mounted elsewhere in /proc/mounts
        3. Software-mount via install script
        4. Fallback: scan mount_base_paths
        """
        # 1. Our fixed mount point
        if os.path.ismount(self.MOUNT_POINT):
            try:
                os.listdir(self.MOUNT_POINT)
                return self.MOUNT_POINT
            except Exception:
                pass

        # 2. Already mounted somewhere else (systemd auto-mount etc.)
        elsewhere = self._mounted_at()
        if elsewhere:
            log.info(f"USB found mounted at {elsewhere}")
            return elsewhere

        # 3. Mount it ourselves
        if os.path.exists(self.MOUNT_SCRIPT):
            if self._try_software_mount():
                return self.MOUNT_POINT

        # 4. Last-resort scan (only consider actual mount points)
        for base in self.mount_base_paths:
            if not os.path.exists(base):
                continue
            try:
                for item in os.listdir(base):
                    full_path = os.path.join(base, item)
                    skip = {"etc", "usr", "var", "bin", "sbin", "lib",
                            "proc", "sys", "dev", "snap"}
                    if (item not in skip
                            and not item.startswith(".")
                            and os.path.ismount(full_path)):
                        try:
                            os.listdir(full_path)
                            return full_path
                        except Exception:
                            continue
            except Exception:
                continue

        return None

    def is_available(self) -> bool:
        """
        Returns True only if a drive is physically plugged in and mounted at
        /mnt/usb and writable.
        """
        mount = self.get_active_mount()
        if not mount:
            log.debug("is_available: get_active_mount returned None")
            return False
        writable = os.access(mount, os.W_OK)
        if not writable:
            log.debug(f"is_available: {mount} exists but is not writable")
        return writable

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

    # ── NEW: DELETE GAME FROM USB ──
    def delete_game(self, filename: str) -> bool:
        """
        Deletes a PGN file from the USB drive.
        Returns True on success, False if missing or error.
        Rejects any path that tries to escape the chess_games subdir.
        """
        mount = self.get_active_mount()
        if not mount:
            return False

        # Safety: strip any directory separators from the filename
        safe_name = os.path.basename(filename)
        if not safe_name or not safe_name.endswith(".pgn"):
            log.warning(f"Rejected delete of non-PGN filename: {filename!r}")
            return False

        full_path = os.path.join(mount, self.subdir, safe_name)
        if not os.path.exists(full_path):
            log.warning(f"Delete failed — file not found: {full_path}")
            return False

        try:
            os.remove(full_path)
            log.info(f"Deleted game from USB: {full_path}")
            return True
        except Exception as e:
            log.error(f"Failed to delete {full_path}: {e}")
            return False

    # ── NEW: IMPORT EXTERNAL PGN ──
    def import_pgn(self, pgn_text: str, source_name: str = "imported") -> str | None:
        """
        Write a PGN string (uploaded from the browser or external source) to
        the chess_games folder on the USB drive.
        Returns the filename on success, None on failure.
        """
        mount = self.get_active_mount()
        if not mount:
            return None

        target_dir = os.path.join(mount, self.subdir)
        os.makedirs(target_dir, exist_ok=True)

        # Sanitise source name for use in filename
        safe_src = "".join(c for c in source_name if c.isalnum() or c in "-_")[:32] or "imported"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename  = f"{timestamp}_{safe_src}.pgn"
        full_path = os.path.join(target_dir, filename)

        try:
            # Quick validation — must parse as valid PGN
            import chess.pgn as _pgn, io as _io
            game = _pgn.read_game(_io.StringIO(pgn_text))
            if not game:
                log.error("import_pgn: text did not parse as valid PGN")
                return None

            with open(full_path, "w", encoding="utf-8") as f:
                f.write(pgn_text)

            log.info(f"Imported PGN to USB: {full_path}")
            return filename
        except Exception as e:
            log.error(f"Failed to import PGN: {e}")
            return None

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