# =============================================================================
# voice.py
# Author : Richard Pu
# Created: 2026-06-12
# Purpose: Optional text-to-speech move announcements.
#          Silently no-ops if no audio device is connected — the board works
#          exactly as before without a speaker plugged in.
#          Tries espeak-ng (fastest, offline, apt-installable) first, then
#          pyttsx3 as a fallback.  Set MOCK_VOICE=1 or DISABLE_VOICE=1 to
#          suppress all speech.
# =============================================================================

import os
import re
import logging
import threading
import subprocess
from queue import Queue, Empty

from config import CFG

log = logging.getLogger(__name__)

_DISABLE = os.environ.get("DISABLE_VOICE", "0") == "1"
_MOCK    = os.environ.get("MOCK_VOICE",    "0") == "1"

# ── File / rank helpers ───────────────────────────────────────────────────────

_FILE_NAME = {"a": "A", "b": "B", "c": "C", "d": "D",
              "e": "E", "f": "F", "g": "G", "h": "H"}

_PIECE_NAME = {"q": "queen", "r": "rook", "b": "bishop", "n": "knight"}


def _uci_to_speech(uci: str, captured: bool = False, is_check: bool = False,
                   is_checkmate: bool = False, promotion: str = "") -> str:
    """
    Convert a UCI string like 'e2e4' into a natural spoken phrase.
    Examples:
        e2e4            → "E 2 to E 4"
        e7e8q           → "E 7 to E 8, promote to queen"
        e4d5  (capture) → "E 4 takes D 5"
    """
    if len(uci) < 4:
        return uci

    f_file = _FILE_NAME.get(uci[0], uci[0].upper())
    f_rank = uci[1]
    t_file = _FILE_NAME.get(uci[2], uci[2].upper())
    t_rank = uci[3]
    promo  = _PIECE_NAME.get(uci[4].lower(), "") if len(uci) >= 5 else promotion

    verb = "takes" if captured else "to"
    phrase = f"{f_file} {f_rank} {verb} {t_file} {t_rank}"

    if promo:
        phrase += f", promote to {promo}"
    if is_checkmate:
        phrase += ". Checkmate!"
    elif is_check:
        phrase += ". Check!"

    return phrase


# ── Backend detection ─────────────────────────────────────────────────────────

def _has_espeak() -> bool:
    try:
        subprocess.run(["espeak-ng", "--version"],
                       capture_output=True, timeout=2)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _has_audio_device() -> bool:
    """Return True if at least one ALSA playback device is present."""
    try:
        result = subprocess.run(["aplay", "-l"],
                                capture_output=True, text=True, timeout=2)
        return "card" in result.stdout
    except Exception:
        return False


# ── Voice engine ──────────────────────────────────────────────────────────────

class VoiceEngine:
    """
    Non-blocking TTS engine.  Speak requests are queued so they never block
    the game loop.  If no audio hardware is available the queue drains silently.
    """

    def __init__(self):
        self._queue: Queue = Queue()
        self._backend: str = "none"
        self._pyttsx3_engine = None

        if _DISABLE:
            log.info("Voice disabled by DISABLE_VOICE=1")
            return

        if _MOCK:
            self._backend = "mock"
            log.info("Voice in MOCK mode — phrases logged only")
        elif not _has_audio_device():
            log.info("No audio device detected — voice disabled (plug in a USB speaker to enable)")
            return
        elif _has_espeak():
            self._backend = "espeak"
            log.info("Voice backend: espeak-ng")
        else:
            try:
                import pyttsx3
                self._pyttsx3_engine = pyttsx3.init()
                self._pyttsx3_engine.setProperty("rate",   CFG.tts_rate)
                self._pyttsx3_engine.setProperty("volume", CFG.tts_volume)
                self._backend = "pyttsx3"
                log.info("Voice backend: pyttsx3")
            except Exception as e:
                log.info(f"No TTS backend available ({e}) — voice disabled")
                return

        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="voice-worker")
        self._thread.start()

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._backend != "none"

    def say(self, text: str):
        """Queue a phrase for async speech. Returns immediately."""
        if self._backend == "none":
            return
        self._queue.put(text)

    def announce_move(self, uci: str, captured: bool = False,
                      is_check: bool = False, is_checkmate: bool = False,
                      promotion: str = ""):
        """Convert a UCI move to speech and queue it."""
        phrase = _uci_to_speech(uci, captured=captured,
                                 is_check=is_check,
                                 is_checkmate=is_checkmate,
                                 promotion=promotion)
        self.say(phrase)

    def announce_status(self, msg: str):
        """Speak a plain status message (e.g. 'Your move', 'New game')."""
        self.say(msg)

    # ── Worker ─────────────────────────────────────────────────────────────────

    def _worker(self):
        while True:
            try:
                text = self._queue.get(timeout=1.0)
            except Empty:
                continue
            self._speak(text)
            self._queue.task_done()

    def _speak(self, text: str):
        if self._backend == "mock":
            log.info(f"[VOICE] {text}")
            return
        try:
            if self._backend == "espeak":
                subprocess.run(
                    ["espeak-ng", "-s", str(CFG.tts_rate), "-a",
                     str(int(CFG.tts_volume * 100)), text],
                    capture_output=True, timeout=10,
                )
            elif self._backend == "pyttsx3" and self._pyttsx3_engine:
                self._pyttsx3_engine.say(text)
                self._pyttsx3_engine.runAndWait()
        except Exception as e:
            log.warning(f"TTS error: {e}")


# Singleton
voice = VoiceEngine()
