# =============================================================================
# web/server.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-12
# Purpose: Real-time web dashboard for the Jetson smart chessboard.
#          Accessible from any device on the same network at http://<ip>:5000.
# =============================================================================

import os
import time
import subprocess
import threading
import logging
from typing import Optional

import chess
import chess.svg

from flask import Flask, render_template_string, jsonify, request, Response
from flask_socketio import SocketIO, emit

from config import CFG

log = logging.getLogger(__name__)

app = Flask(__name__)

_secret = os.environ.get("FLASK_SECRET_KEY", "")
if not _secret:
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set. "
        "Run:  python3 -c \"import secrets; print(secrets.token_hex(32))\"  "
        "then add FLASK_SECRET_KEY=<value> to your .env file."
    )
app.config["SECRET_KEY"] = _secret

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Shared state ───────────────────────────────────────────────────────────────

_state = {
    "fen":           chess.STARTING_FEN,
    "move_history":  [],
    "whose_turn":    "White",
    "game_mode":     "—",
    "difficulty":    0,
    "last_move":     None,
    "status":        "Waiting to start",
    "thinking":      False,
    "theme":         "classic",
    "custom_theme":  None,
    "eval_score":    0,
    "hint_move":     "",
    "game_active":   False,
    "usb_available": False,
    "voice_enabled": False,
}
_state_lock = threading.Lock()

_callbacks = {
    "new_game":     None,
    "hint":         None,
    "set_theme":    None,
    "move_input":   None,
    "undo":         None,
    "save_usb":     None,
    "toggle_voice": None,
}


# ── Public API (called from main.py) ───────────────────────────────────────────

def update_state(**kwargs):
    with _state_lock:
        _state.update(kwargs)
    _push_state()


def register_callbacks(**kw):
    for k, v in kw.items():
        if k in _callbacks:
            _callbacks[k] = v


def start_server(host: Optional[str] = None, port: Optional[int] = None):
    h = host or CFG.web_host
    p = port or CFG.web_port
    t = threading.Thread(
        target=lambda: socketio.run(app, host=h, port=p,
                                    allow_unsafe_werkzeug=True),
        daemon=True,
        name="web-dashboard",
    )
    t.start()
    log.info(f"Web dashboard running at http://{h}:{p}")


# ── Socket push ────────────────────────────────────────────────────────────────

def _push_state():
    with _state_lock:
        payload = {
            "fen":           _state["fen"],
            "move_history":  _state["move_history"][-60:],
            "whose_turn":    _state["whose_turn"],
            "game_mode":     _state["game_mode"],
            "difficulty":    _state["difficulty"],
            "status":        _state["status"],
            "thinking":      _state["thinking"],
            "theme":         _state["theme"],
            "eval_score":    _state["eval_score"],
            "game_active":   _state["game_active"],
            "move_count":    len(_state["move_history"]),
            "usb_available": _state["usb_available"],
            "voice_enabled": _state["voice_enabled"],
            "last_move":     _state["last_move"] or "",
            "hint_move":     _state["hint_move"] or "",
        }

    socketio.emit("state_update", payload)


# ── HTTP Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/state")
def api_state():
    with _state_lock:
        return jsonify({k: v for k, v in _state.items()
                        if k not in ("last_move",)})


@app.route("/api/system")
def api_system():
    info = {}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            info["cpu_temp_c"] = int(f.read().strip()) // 1000
    except Exception:
        info["cpu_temp_c"] = "N/A"
    try:
        import psutil
        info["cpu_pct"]      = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        info["ram_used_mb"]  = mem.used  // 1024 // 1024
        info["ram_total_mb"] = mem.total // 1024 // 1024
    except Exception:
        info["cpu_pct"] = info["ram_used_mb"] = info["ram_total_mb"] = "N/A"
    return jsonify(info)


@app.route("/api/pgn/download")
def api_pgn_download():
    with _state_lock:
        history = list(_state["move_history"])
        mode    = _state["game_mode"]
        diff    = _state["difficulty"]

    import chess.pgn
    from datetime import datetime
    import io

    game = chess.pgn.Game()
    game.headers["Event"]  = "Jetson Smart Chess Board"
    game.headers["Site"]   = "Local"
    game.headers["Date"]   = datetime.now().strftime("%Y.%m.%d")
    game.headers["White"]  = "Human"
    game.headers["Black"]  = mode
    game.headers["Result"] = "*"
    if diff:
        game.headers["BlackElo"] = str(diff * 100)

    node  = game
    board = chess.Board()
    for uci in history:
        try:
            move = chess.Move.from_uci(uci)
            node = node.add_variation(move)
            board.push(move)
        except Exception:
            pass

    buf = io.StringIO()
    game.accept(chess.pgn.FileExporter(buf))
    pgn_text = buf.getvalue()
    filename = datetime.now().strftime("chess_%Y-%m-%d_%H-%M.pgn")
    return Response(
        pgn_text,
        mimetype="application/x-chess-pgn",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/api/update", methods=["POST"])
def api_update():
    ota_token = os.environ.get("OTA_TOKEN", "")
    if not ota_token:
        return jsonify({"error": "OTA_TOKEN not set in .env — update disabled"}), 403
    data = request.get_json(silent=True) or {}
    if data.get("token") != ota_token:
        return jsonify({"error": "Invalid OTA token"}), 403
    try:
        result = subprocess.run(
            ["git", "pull"], capture_output=True, text=True, timeout=60,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        output = result.stdout + result.stderr
        log.info(f"OTA git pull:\n{output}")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if data.get("restart"):
        threading.Thread(
            target=lambda: (time.sleep(1),
                            subprocess.run(["systemctl", "restart", "chess"],
                                           capture_output=True)),
            daemon=True,
        ).start()
        return jsonify({"output": output, "restarting": True})
    return jsonify({"output": output, "restarting": False})


# ── Socket events ──────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
  log.info(f"Dashboard client connected: {getattr(request, 'sid', '')}")
  _push_state()

@socketio.on("new_game")
def on_new_game():
    if _callbacks["new_game"]:
        threading.Thread(target=_callbacks["new_game"], daemon=True).start()

@socketio.on("hint")
def on_hint():
    if _callbacks["hint"]:
        threading.Thread(target=_callbacks["hint"], daemon=True).start()

@socketio.on("undo")
def on_undo():
    if _callbacks["undo"]:
        threading.Thread(target=_callbacks["undo"], daemon=True).start()

@socketio.on("save_usb")
def on_save_usb():
    if _callbacks["save_usb"]:
        threading.Thread(target=_callbacks["save_usb"], daemon=True).start()

@socketio.on("toggle_voice")
def on_toggle_voice():
    if _callbacks["toggle_voice"]:
        threading.Thread(target=_callbacks["toggle_voice"], daemon=True).start()

@socketio.on("set_theme")
def on_set_theme(data):
    theme = data.get("theme", "classic")
    update_state(theme=theme)
    if _callbacks["set_theme"]:
        _callbacks["set_theme"](theme)

@socketio.on("set_custom_theme")
def on_set_custom_theme(data):
    with _state_lock:
        _state["theme"]        = "custom"
        _state["custom_theme"] = data
    if _callbacks["set_theme"]:
        _callbacks["set_theme"](data)
    _push_state()

@socketio.on("get_legal_moves")
def on_get_legal_moves(data):
    """Return all legal UCI moves from a given square in the current FEN."""
    try:
        fen = data.get("fen") or chess.STARTING_FEN
        sq_alg = data.get("sq", "")
        board = chess.Board(fen)
        # Find the square index
        sq = chess.parse_square(sq_alg)
        moves = [
            m.uci() for m in board.legal_moves
            if m.from_square == sq
        ]
        emit("legal_moves", {"moves": moves, "sq": sq_alg})
    except Exception as e:
        emit("legal_moves", {"moves": [], "sq": data.get("sq", "")})


@socketio.on("move_input")
def on_move_input(data):
    uci = (data.get("uci") or "").strip().lower()
    if len(uci) < 4:
        emit("move_rejected", {"reason": "Too short"})
        return
    log.info(f"Move input from dashboard: {uci}")
    if _callbacks["move_input"]:
        threading.Thread(target=_callbacks["move_input"],
                         args=(uci,), daemon=True).start()


@socketio.on("get_dev_settings")
def on_get_dev_settings():
    """Return all live-editable CFG values to the dev menu."""
    emit("dev_settings", {
        "difficulty_default":  CFG.difficulty_default,
        "difficulty_min":      CFG.difficulty_min,
        "difficulty_max":      CFG.difficulty_max,
        "movetime_default_ms": CFG.movetime_default_ms,
        "movetime_min_ms":     CFG.movetime_min_ms,
        "movetime_max_ms":     CFG.movetime_max_ms,
        "chess_led_brightness":CFG.chess_led_brightness,
        "startup_led_delay_s": CFG.startup_led_delay_s,
        "button_debounce_s":   CFG.button_debounce_s,
        "hint_dismiss_s":      CFG.hint_dismiss_s,
        "undo_max_half_moves": CFG.undo_max_half_moves,
        "stockfish_path":      CFG.stockfish_path,
        "tts_rate":            CFG.tts_rate,
        "tts_volume":          CFG.tts_volume,
        "web_port":            CFG.web_port,
        "pgn_subdir":          CFG.pgn_subdir,
    })


@socketio.on("apply_dev_settings")
def on_apply_dev_settings(data):
    """
    Live-apply developer settings to the running CFG singleton.
    Changes take effect immediately for the current session.
    They are NOT written back to .env — restart reverts them.
    """
    changed = []
    def _set(attr, key, cast):
        val = data.get(key)
        if val is not None:
            try:
                setattr(CFG, attr, cast(val))
                changed.append(f"{attr}={getattr(CFG, attr)}")
            except Exception as e:
                log.warning(f"Dev setting {key}: {e}")

    _set("difficulty_default",   "difficulty_default",   int)
    _set("difficulty_min",       "difficulty_min",        int)
    _set("difficulty_max",       "difficulty_max",        int)
    _set("movetime_default_ms",  "movetime_default_ms",   int)
    _set("movetime_min_ms",      "movetime_min_ms",       int)
    _set("movetime_max_ms",      "movetime_max_ms",       int)
    _set("chess_led_brightness", "chess_led_brightness",  int)
    _set("startup_led_delay_s",  "startup_led_delay_s",   float)
    _set("button_debounce_s",    "button_debounce_s",     float)
    _set("hint_dismiss_s",       "hint_dismiss_s",        float)
    _set("undo_max_half_moves",  "undo_max_half_moves",   int)
    _set("stockfish_path",       "stockfish_path",        str)
    _set("tts_rate",             "tts_rate",              int)
    _set("tts_volume",           "tts_volume",            float)
    _set("web_port",             "web_port",              int)
    _set("pgn_subdir",           "pgn_subdir",            str)

    log.info(f"Dev settings applied: {', '.join(changed)}")
    emit("dev_settings_saved", {
        "ok":      True,
        "changed": changed,
        "note":    "Settings are live for this session. Restart to revert.",
    })


# ── Dashboard HTML ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chess Board — Control</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;600&family=Google+Sans+Mono&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
/* ── Design tokens ── */
:root {
  --gold:       #c9a84c;
  --gold-dim:   #9a7a30;
  --gold-glow:  rgba(201,168,76,0.15);
  --sq-light:   #f0d9b5;
  --sq-dark:    #b58863;
  --sq-sel:     rgba(201,168,76,0.70);
  --sq-legal:   rgba(201,168,76,0.35);
  --sq-last:    rgba(100,170,80,0.45);
  --sq-hint:    rgba(60,160,220,0.50);
  --font-mono:  'Google Sans Mono', 'JetBrains Mono', 'Fira Mono', monospace;
  --font-sans:  'Google Sans', 'Product Sans', system-ui, sans-serif;
  --r:          8px;
  --r-lg:       14px;
  --transition: 0.18s ease;
}

[data-theme="dark"] {
  --bg:         #111418;
  --surface:    #191d23;
  --surface2:   #20252d;
  --border:     rgba(255,255,255,0.08);
  --border-hov: rgba(255,255,255,0.15);
  --text:       #e8e6e1;
  --text-2:     #9a9690;
  --text-3:     #5a5855;
  --badge-bg:   rgba(255,255,255,0.06);
}

[data-theme="light"] {
  --bg:         #f4f1ec;
  --surface:    #ffffff;
  --surface2:   #f0ede8;
  --border:     rgba(0,0,0,0.10);
  --border-hov: rgba(0,0,0,0.20);
  --text:       #1a1713;
  --text-2:     #6b6460;
  --text-3:     #b0ada8;
  --badge-bg:   rgba(0,0,0,0.05);
}

/* ── Reset & base ── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 15px; }
body {
  font-family: var(--font-sans);
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

/* ── Topbar ── */
.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 20px;
  height: 56px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 100;
}
.topbar-logo {
  font-size: 1.15rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--gold);
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}
.topbar-logo svg { width: 22px; height: 22px; flex-shrink: 0; }
.topbar-spacer { flex: 1; }
.topbar-badges { display: flex; gap: 8px; align-items: center; }
.badge {
  font-size: 0.72rem;
  font-weight: 500;
  padding: 3px 9px;
  border-radius: 20px;
  background: var(--badge-bg);
  border: 1px solid var(--border);
  color: var(--text-2);
  white-space: nowrap;
}
.badge.gold  { background: var(--gold-glow); border-color: var(--gold-dim); color: var(--gold); }
.badge.green { background: rgba(80,190,100,0.12); border-color: rgba(80,190,100,0.35); color: #50be64; }
.badge.red   { background: rgba(220,70,70,0.12);  border-color: rgba(220,70,70,0.35);  color: #dc4646; }
.conn-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #dc4646;
  transition: background var(--transition);
  flex-shrink: 0;
}
.conn-dot.live { background: #50be64; }
.topbar-btn {
  background: none; border: 1px solid var(--border);
  border-radius: var(--r); padding: 5px 10px; cursor: pointer;
  color: var(--text-2); font-size: 0.78rem; transition: all var(--transition);
  display: flex; align-items: center; gap: 5px;
}
.topbar-btn:hover { border-color: var(--border-hov); color: var(--text); }

/* ── Layout ── */
.layout {
  display: grid;
  grid-template-columns: 1fr 340px;
  grid-template-rows: auto;
  gap: 16px;
  padding: 16px;
  max-width: 1120px;
  margin: 0 auto;
}
@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
}

/* ── Cards ── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  padding: 16px;
}
.card-title {
  font-size: 0.70rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-3);
  margin-bottom: 12px;
}

/* ── Chess board canvas ── */
#board-wrap {
  position: relative;
  width: 100%;
  max-width: 520px;
  margin: 0 auto;
}
#board-canvas {
  display: block;
  width: 100%;
  aspect-ratio: 1;
  border-radius: var(--r);
  cursor: pointer;
  touch-action: none;
}
.board-coords {
  position: absolute;
  font-family: var(--font-mono);
  font-size: 0.6rem;
  color: var(--text-3);
  pointer-events: none;
  font-weight: 500;
  letter-spacing: 0.02em;
}

/* ── Eval bar ── */
.eval-wrap { width: 100%; margin-top: 10px; }
.eval-track {
  height: 6px;
  border-radius: 3px;
  background: var(--surface2);
  overflow: hidden;
  position: relative;
}
.eval-white {
  position: absolute; left: 0; top: 0; bottom: 0;
  background: var(--sq-light);
  transition: width 0.5s cubic-bezier(0.4,0,0.2,1);
}
.eval-black {
  position: absolute; right: 0; top: 0; bottom: 0;
  background: var(--sq-dark);
  transition: width 0.5s cubic-bezier(0.4,0,0.2,1);
}
.eval-label {
  display: flex; justify-content: space-between;
  font-size: 0.68rem; color: var(--text-3);
  font-family: var(--font-mono);
  margin-top: 4px;
}

/* ── Turn banner ── */
.turn-banner {
  width: 100%;
  padding: 10px 14px;
  border-radius: var(--r);
  font-weight: 600;
  font-size: 0.88rem;
  display: flex;
  align-items: center;
  gap: 8px;
  transition: all var(--transition);
  margin-top: 10px;
}
.turn-banner.white { background: rgba(240,217,181,0.15); border: 1px solid rgba(240,217,181,0.30); color: var(--sq-light); }
.turn-banner.black { background: rgba(100,80,60,0.20);   border: 1px solid rgba(100,80,60,0.40);   color: #b09070; }
.turn-banner.thinking {
  background: var(--gold-glow);
  border: 1px solid var(--gold-dim);
  color: var(--gold);
  animation: think-pulse 1.2s ease-in-out infinite;
}
@keyframes think-pulse { 0%,100%{opacity:1} 50%{opacity:0.6} }
.turn-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
}
.turn-banner.white .turn-dot { background: #f0d9b5; }
.turn-banner.black .turn-dot { background: #b09070; }
.turn-banner.thinking .turn-dot {
  background: var(--gold);
  animation: think-dot 0.6s ease-in-out infinite alternate;
}
@keyframes think-dot { from{transform:scale(1)} to{transform:scale(1.6)} }

/* ── Status line ── */
.status-line {
  font-size: 0.80rem;
  color: var(--text-2);
  padding: 8px 12px;
  background: var(--surface2);
  border-radius: var(--r);
  margin-top: 8px;
  min-height: 34px;
  display: flex;
  align-items: center;
  font-family: var(--font-mono);
  letter-spacing: 0.01em;
  border: 1px solid var(--border);
}

/* ── Right column ── */
.right-col { display: flex; flex-direction: column; gap: 12px; }

/* ── Stats grid ── */
.stats-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.stat-cell {
  background: var(--surface2);
  border-radius: var(--r);
  padding: 10px 12px;
  border: 1px solid var(--border);
}
.stat-label { font-size: 0.65rem; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }
.stat-value { font-family: var(--font-mono); font-size: 1.05rem; font-weight: 600; color: var(--gold); }

/* ── Move history ── */
.move-history {
  max-height: 200px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 0.78rem;
}
.move-history::-webkit-scrollbar { width: 3px; }
.move-history::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.move-history::-webkit-scrollbar-track { background: transparent; }
.move-row {
  display: grid;
  grid-template-columns: 28px 1fr 1fr;
  gap: 6px;
  padding: 3px 0;
  border-bottom: 1px solid var(--border);
  align-items: center;
}
.move-row:last-child { border-bottom: none; }
.move-num { color: var(--text-3); font-size: 0.70rem; }
.move-w, .move-b { color: var(--text-2); }
.move-row.latest .move-w,
.move-row.latest .move-b { color: var(--gold); font-weight: 600; }
.move-empty { color: var(--text-3); font-size: 0.78rem; font-style: italic; }

/* ── Buttons ── */
.btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
.btn {
  padding: 8px 14px;
  border-radius: var(--r);
  border: 1px solid var(--border);
  background: var(--surface2);
  color: var(--text);
  cursor: pointer;
  font-size: 0.82rem;
  font-family: var(--font-sans);
  transition: all var(--transition);
  display: flex; align-items: center; gap: 6px;
  white-space: nowrap;
}
.btn:hover { border-color: var(--border-hov); background: var(--surface); }
.btn:active { transform: scale(0.97); }
.btn:disabled { opacity: 0.35; pointer-events: none; }
.btn.primary {
  background: var(--gold-glow);
  border-color: var(--gold-dim);
  color: var(--gold);
  font-weight: 600;
}
.btn.primary:hover { background: rgba(201,168,76,0.22); border-color: var(--gold); }
.btn.danger { background: rgba(220,70,70,0.10); border-color: rgba(220,70,70,0.3); color: #dc7070; }
.btn.danger:hover { background: rgba(220,70,70,0.18); }
.btn.sm { padding: 5px 10px; font-size: 0.75rem; }
.btn-full { width: 100%; justify-content: center; }

/* ── Theme selector ── */
.theme-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 6px;
  margin-bottom: 10px;
}
.theme-swatch {
  aspect-ratio: 1;
  border-radius: 6px;
  cursor: pointer;
  border: 2px solid transparent;
  transition: all var(--transition);
  display: flex; align-items: center; justify-content: center;
  font-size: 0.6rem; font-weight: 600; color: rgba(255,255,255,0.7);
  overflow: hidden;
  position: relative;
}
.theme-swatch:hover { transform: scale(1.05); }
.theme-swatch.active { border-color: var(--gold); }
.theme-swatch span {
  position: absolute; bottom: 3px; left: 0; right: 0;
  text-align: center; font-size: 0.55rem; font-weight: 600;
  text-shadow: 0 1px 2px rgba(0,0,0,0.6);
}

.custom-theme-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 10px;
}
.color-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.75rem;
  color: var(--text-2);
}
.color-row input[type=color] {
  width: 28px; height: 28px; padding: 2px;
  border-radius: 4px; border: 1px solid var(--border);
  background: var(--surface2); cursor: pointer;
  flex-shrink: 0;
}

/* ── Sys info ── */
.sys-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 5px 0;
  border-bottom: 1px solid var(--border);
  font-size: 0.78rem;
}
.sys-row:last-child { border-bottom: none; }
.sys-key { color: var(--text-2); }
.sys-val { font-family: var(--font-mono); color: var(--gold); font-size: 0.78rem; }

/* ── Toggle switch ── */
.toggle-wrap { display: flex; align-items: center; gap: 10px; }
.toggle {
  position: relative; width: 36px; height: 20px; flex-shrink: 0;
}
.toggle input { opacity: 0; width: 0; height: 0; position: absolute; }
.toggle-track {
  position: absolute; inset: 0;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 10px;
  cursor: pointer;
  transition: all var(--transition);
}
.toggle input:checked + .toggle-track { background: var(--gold-glow); border-color: var(--gold-dim); }
.toggle-thumb {
  position: absolute; top: 2px; left: 2px;
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--text-3);
  transition: all var(--transition);
  pointer-events: none;
}
.toggle input:checked ~ .toggle-thumb { transform: translateX(16px); background: var(--gold); }
.toggle-label { font-size: 0.82rem; color: var(--text-2); }

/* ── OTA output ── */
.ota-out {
  display: none;
  background: #0d1117;
  border-radius: 6px;
  padding: 10px;
  font-family: var(--font-mono);
  font-size: 0.70rem;
  color: #58a65c;
  max-height: 120px;
  overflow-y: auto;
  margin-top: 8px;
  border: 1px solid rgba(88,166,92,0.2);
  white-space: pre-wrap;
}

/* ── Section divider ── */
.divider { height: 1px; background: var(--border); margin: 10px 0; }

/* ── Promotion modal ── */
.promo-overlay {
  display: none;
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.7);
  z-index: 200;
  align-items: center;
  justify-content: center;
  backdrop-filter: blur(4px);
}
.promo-overlay.show { display: flex; }
.promo-modal {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  padding: 24px;
  text-align: center;
  min-width: 280px;
}
.promo-modal h3 { font-size: 0.9rem; color: var(--text-2); margin-bottom: 16px; font-weight: 500; }
.promo-pieces {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}
.promo-piece {
  aspect-ratio: 1;
  border-radius: var(--r);
  background: var(--surface2);
  border: 1px solid var(--border);
  cursor: pointer;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 4px;
  font-size: 2.2rem;
  transition: all var(--transition);
}
.promo-piece span { font-size: 0.62rem; color: var(--text-3); font-family: var(--font-mono); }
.promo-piece:hover { border-color: var(--gold-dim); background: var(--gold-glow); }

/* ── Toast notifications ── */
.toast-wrap {
  position: fixed; bottom: 20px; right: 20px;
  display: flex; flex-direction: column; gap: 8px;
  z-index: 300; pointer-events: none;
}
.toast {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r);
  padding: 10px 16px;
  font-size: 0.82rem;
  color: var(--text);
  opacity: 0;
  transform: translateY(8px);
  transition: all 0.25s ease;
  pointer-events: auto;
  max-width: 260px;
}
.toast.show { opacity: 1; transform: none; }
.toast.ok   { border-color: rgba(80,190,100,0.4); background: rgba(80,190,100,0.08); }
.toast.err  { border-color: rgba(220,70,70,0.4);  background: rgba(220,70,70,0.08);  color: #e07070; }

/* ── Responsive overrides ── */
@media (max-width: 600px) {
  .topbar { padding: 0 12px; }
  .topbar-badges .badge:not(:last-child):not(.conn-label) { display: none; }
  .layout { padding: 10px; gap: 10px; }
  .card { padding: 12px; }
}
</style>
</head>
<body>

<!-- Topbar -->
<header class="topbar">
  <div class="topbar-logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M9 2h6l-1 4H10L9 2z"/>
      <path d="M10 6c0 2-2 3-2 5h8c0-2-2-3-2-5"/>
      <rect x="8" y="11" width="8" height="2" rx="1"/>
      <path d="M7 13v3l-1 3h10l-1-3v-3"/>
    </svg>
    Smart Chess
  </div>
  <div class="topbar-spacer"></div>
  <div class="topbar-badges">
    <span class="badge" id="badge-mode">—</span>
    <span class="badge" id="badge-turn">—</span>
    <div class="conn-dot" id="conn-dot" title="Connection status"></div>
  </div>
  <button class="topbar-btn" id="theme-toggle-btn" onclick="toggleTheme()" title="Toggle light/dark">
    <svg id="theme-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
      <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
    </svg>
  </button>
</header>

<!-- Promotion modal -->
<div class="promo-overlay" id="promo-overlay">
  <div class="promo-modal">
    <h3>Promote pawn to…</h3>
    <div class="promo-pieces">
      <div class="promo-piece" onclick="submitPromotion('q')">♛<span>Queen</span></div>
      <div class="promo-piece" onclick="submitPromotion('r')">♜<span>Rook</span></div>
      <div class="promo-piece" onclick="submitPromotion('b')">♝<span>Bishop</span></div>
      <div class="promo-piece" onclick="submitPromotion('n')">♞<span>Knight</span></div>
    </div>
  </div>
</div>

<!-- Toast container -->
<div class="toast-wrap" id="toast-wrap"></div>

<!-- Main layout -->
<main class="layout">

  <!-- Left: board column -->
  <div style="display:flex;flex-direction:column;gap:12px;">

    <div class="card">
      <div class="card-title">Board</div>
      <div id="board-wrap">
        <canvas id="board-canvas"></canvas>
      </div>
      <div class="eval-wrap">
        <div class="eval-track">
          <div class="eval-white" id="eval-white" style="width:50%"></div>
        </div>
        <div class="eval-label">
          <span id="eval-w-label">0.00</span>
          <span style="color:var(--text-3)">evaluation</span>
          <span id="eval-b-label">0.00</span>
        </div>
      </div>
      <div class="turn-banner white" id="turn-banner">
        <div class="turn-dot"></div>
        <span id="turn-text">White's turn</span>
      </div>
      <div class="status-line" id="status-line">Waiting for board...</div>
    </div>

    <div class="card">
      <div class="card-title">Controls</div>
      <div class="btn-row">
        <button class="btn primary" onclick="doAction('new_game')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4"/></svg>
          New game
        </button>
        <button class="btn" onclick="doAction('hint')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          Hint
        </button>
        <button class="btn" onclick="doAction('undo')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/></svg>
          Undo
        </button>
      </div>
    </div>

  </div>

  <!-- Right column -->
  <div class="right-col">

    <!-- Game info -->
    <div class="card">
      <div class="card-title">Session</div>
      <div class="stats-grid">
        <div class="stat-cell"><div class="stat-label">Mode</div><div class="stat-value" id="s-mode">—</div></div>
        <div class="stat-cell"><div class="stat-label">Difficulty</div><div class="stat-value" id="s-diff">—</div></div>
        <div class="stat-cell"><div class="stat-label">Moves</div><div class="stat-value" id="s-moves">0</div></div>
        <div class="stat-cell"><div class="stat-label">Turn</div><div class="stat-value" id="s-turn">—</div></div>
      </div>
    </div>

    <!-- Move history -->
    <div class="card">
      <div class="card-title">Move history</div>
      <div class="move-history" id="move-list">
        <span class="move-empty">No moves yet</span>
      </div>
    </div>

    <!-- LED themes -->
    <div class="card">
      <div class="card-title">LED theme</div>
      <div class="theme-grid" id="theme-grid"></div>
      <div id="custom-panel" style="display:none;">
        <div class="divider"></div>
        <div class="custom-theme-grid">
          <div class="color-row"><input type="color" id="c-light" value="#ffffff" oninput="liveCustomTheme()"> Light sq.</div>
          <div class="color-row"><input type="color" id="c-dark"  value="#000000" oninput="liveCustomTheme()"> Dark sq.</div>
          <div class="color-row"><input type="color" id="c-move"  value="#00ff00" oninput="liveCustomTheme()"> Move</div>
          <div class="color-row"><input type="color" id="c-hint"  value="#00ffff" oninput="liveCustomTheme()"> Hint</div>
        </div>
        <button class="btn btn-full" style="margin-top:8px;" onclick="applyCustom()">Apply to LEDs</button>
      </div>
    </div>

    <!-- Save & export -->
    <div class="card">
      <div class="card-title">Save & export</div>
      <div class="btn-row">
        <button class="btn" id="usb-btn" onclick="doAction('save_usb')" disabled>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
          Save to USB
        </button>
        <a href="/api/pgn/download" style="text-decoration:none;">
          <button class="btn">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            PGN
          </button>
        </a>
      </div>
      <div style="font-size:0.72rem;color:var(--text-3);margin-top:6px;" id="usb-status">No USB drive detected</div>
    </div>

    <!-- Voice -->
    <div class="card">
      <div class="card-title">Voice</div>
      <div class="toggle-wrap">
        <label class="toggle">
          <input type="checkbox" id="voice-toggle" onchange="doAction('toggle_voice')">
          <div class="toggle-track"></div>
          <div class="toggle-thumb"></div>
        </label>
        <span class="toggle-label" id="voice-label">Announcements off</span>
      </div>
      <div style="font-size:0.70rem;color:var(--text-3);margin-top:6px;">
        Plug in a USB speaker to enable audio.
      </div>
    </div>

    <!-- System -->
    <div class="card">
      <div class="card-title">Jetson system</div>
      <div class="sys-row"><span class="sys-key">CPU temp</span><span class="sys-val" id="sys-temp">—</span></div>
      <div class="sys-row"><span class="sys-key">CPU load</span><span class="sys-val" id="sys-cpu">—</span></div>
      <div class="sys-row"><span class="sys-key">RAM used</span><span class="sys-val" id="sys-ram">—</span></div>
      <div class="divider"></div>
      <button class="btn btn-full sm" onclick="otaUpdate()">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4"/></svg>
        Check for updates
      </button>
      <div class="ota-out" id="ota-out"></div>
    </div>

  </div>
</main>

<script>
// ── Theme (light/dark) ─────────────────────────────────────────────────────
const prefersDark = matchMedia('(prefers-color-scheme: dark)').matches;
let currentTheme = localStorage.getItem('dash-theme') || (prefersDark ? 'dark' : 'light');
document.documentElement.setAttribute('data-theme', currentTheme);

function toggleTheme() {
  currentTheme = currentTheme === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', currentTheme);
  localStorage.setItem('dash-theme', currentTheme);
  drawBoard();
}

// ── Chess board canvas ─────────────────────────────────────────────────────
const canvas  = document.getElementById('board-canvas');
const ctx     = canvas.getContext('2d');
let   currentFen   = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
let   legalMoves   = [];   // list of UCI strings for selected piece
let   selectedSq   = null; // 0-63 index
let   dragPiece    = null; // { sq, x, y }
let   lastMoveSqs  = [];   // [from, to] indices
let   hintSqs      = [];   // [from, to] indices
let   pendingPromoUci = null;
let   boardFlipped = false;

// Piece unicode map (uses text rendering — crisp on all displays)
const PIECES = {
  'K':'♔','Q':'♕','R':'♖','B':'♗','N':'♘','P':'♙',
  'k':'♚','q':'♛','r':'♜','b':'♝','n':'♞','p':'♟'
};

// Parse FEN into an 8×8 array (rank 8 top = index 0)
function parseFen(fen) {
  const board = new Array(64).fill(null);
  const rows  = fen.split(' ')[0].split('/');
  rows.forEach((row, rank) => {
    let file = 0;
    for (const ch of row) {
      if (ch >= '1' && ch <= '8') { file += parseInt(ch); }
      else { board[rank * 8 + file] = ch; file++; }
    }
  });
  return board;
}

// Convert sq index ↔ algebraic
function sqToAlg(sq) {
  const file = sq % 8;
  const rank = 7 - Math.floor(sq / 8);
  return 'abcdefgh'[file] + (rank + 1);
}
function algToSq(alg) {
  const file = 'abcdefgh'.indexOf(alg[0]);
  const rank = parseInt(alg[1]) - 1;
  return (7 - rank) * 8 + file;
}

// Compute display square from board index (handles flip)
function displaySq(sq) {
  return boardFlipped ? 63 - sq : sq;
}

function getColors() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  return {
    light:    dark ? '#b0956e' : '#f0d9b5',
    dark:     dark ? '#6e4e2e' : '#b58863',
    sel:      'rgba(201,168,76,0.70)',
    legal:    'rgba(201,168,76,0.30)',
    legalCap: 'rgba(201,168,76,0.55)',
    lastMove: dark ? 'rgba(90,150,70,0.50)' : 'rgba(120,190,90,0.45)',
    hint:     dark ? 'rgba(50,140,210,0.55)' : 'rgba(60,160,220,0.50)',
    coord:    dark ? 'rgba(255,255,255,0.25)' : 'rgba(0,0,0,0.25)',
  };
}

function drawBoard(highlightMoves) {
  const DPR  = window.devicePixelRatio || 1;
  const SIZE = canvas.parentElement.clientWidth;
  canvas.width  = SIZE * DPR;
  canvas.height = SIZE * DPR;
  canvas.style.width  = SIZE + 'px';
  canvas.style.height = SIZE + 'px';
  ctx.scale(DPR, DPR);

  const SQ   = SIZE / 8;
  const cols = getColors();
  const board = parseFen(currentFen);

  // Build legal-target set
  const legalTargets = new Set();
  const legalCaptures = new Set();
  legalMoves.forEach(uci => {
    const tsq = algToSq(uci.slice(2, 4));
    legalTargets.add(tsq);
    if (board[tsq]) legalCaptures.add(tsq);
  });

  for (let i = 0; i < 64; i++) {
    const di   = displaySq(i);
    const row  = Math.floor(di / 8);
    const file = di % 8;
    const x    = file * SQ;
    const y    = row  * SQ;
    const isLight = (Math.floor(i / 8) + i % 8) % 2 === 0;

    // Base square colour
    ctx.fillStyle = isLight ? cols.light : cols.dark;
    ctx.fillRect(x, y, SQ, SQ);

    // Last move highlight
    if (lastMoveSqs.includes(i)) {
      ctx.fillStyle = cols.lastMove;
      ctx.fillRect(x, y, SQ, SQ);
    }
    // Hint highlight
    if (hintSqs.includes(i)) {
      ctx.fillStyle = cols.hint;
      ctx.fillRect(x, y, SQ, SQ);
    }
    // Selected
    if (selectedSq === i) {
      ctx.fillStyle = cols.sel;
      ctx.fillRect(x, y, SQ, SQ);
    }

    // Legal move indicator
    if (legalTargets.has(i) && highlightMoves !== false) {
      if (legalCaptures.has(i)) {
        // Ring for captures
        ctx.strokeStyle = cols.legalCap;
        ctx.lineWidth   = SQ * 0.08;
        ctx.beginPath();
        ctx.arc(x + SQ/2, y + SQ/2, SQ * 0.45, 0, Math.PI * 2);
        ctx.stroke();
      } else {
        // Dot for empty squares
        ctx.fillStyle = cols.legal;
        ctx.beginPath();
        ctx.arc(x + SQ/2, y + SQ/2, SQ * 0.16, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // Coordinate labels
    ctx.font        = `${Math.round(SQ * 0.2)}px JetBrains Mono, monospace`;
    ctx.fillStyle   = cols.coord;
    ctx.textBaseline = 'top';
    if (file === 0) { ctx.textAlign = 'left';  ctx.fillText(8 - Math.floor(i/8), x+3, y+2); }
    if (row  === 7) { ctx.textAlign = 'right'; ctx.fillText('abcdefgh'[i%8], x+SQ-3, y+SQ-Math.round(SQ*0.22)); }

    // Piece (skip if being dragged)
    const piece = board[i];
    if (piece && !(dragPiece && dragPiece.sq === i)) {
      drawPiece(piece, x + SQ/2, y + SQ/2, SQ);
    }
  }

  // Dragged piece follows pointer
  if (dragPiece) {
    drawPiece(dragPiece.piece, dragPiece.x, dragPiece.y, SQ * 1.15);
  }
}

function drawPiece(piece, cx, cy, size) {
  const isWhite = piece === piece.toUpperCase();
  ctx.font      = `${Math.round(size * 0.72)}px serif`;
  ctx.textAlign    = 'center';
  ctx.textBaseline = 'middle';
  // Shadow for contrast on any square
  ctx.shadowColor  = isWhite ? 'rgba(0,0,0,0.6)' : 'rgba(0,0,0,0.4)';
  ctx.shadowBlur   = size * 0.06;
  ctx.fillStyle    = isWhite ? '#fffaf2' : '#1a1008';
  ctx.fillText(PIECES[piece] || piece, cx, cy + size * 0.02);
  ctx.shadowBlur   = 0;
}

// ── Board interaction ──────────────────────────────────────────────────────

function getBoardSq(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  const SQ   = rect.width / 8;
  const file = Math.floor((clientX - rect.left) / SQ);
  const rank = Math.floor((clientY - rect.top)  / SQ);
  if (file < 0 || file > 7 || rank < 0 || rank > 7) return null;
  return displaySq(rank * 8 + file);
}

function fetchLegalMoves(sq) {
  const board = parseFen(currentFen);
  const piece = board[sq];
  if (!piece) return;

  // Build legal moves locally using chess.js-like logic
  // We rely on the server's FEN and ask for legal moves via socket
  socket.emit('get_legal_moves', { fen: currentFen, sq: sqToAlg(sq) });
}

function handleSquareClick(sq) {
  if (sq === null) return;
  const board = parseFen(currentFen);
  const piece = board[sq];

  if (selectedSq === null) {
    // Select a piece
    if (piece) {
      selectedSq = sq;
      legalMoves = [];
      fetchLegalMoves(sq);
      drawBoard();
    }
  } else if (selectedSq === sq) {
    // Deselect
    selectedSq = null; legalMoves = []; drawBoard();
  } else {
    // Try a move
    const uci = sqToAlg(selectedSq) + sqToAlg(sq);
    // Check for promotion
    const movingPiece = board[selectedSq];
    if ((movingPiece === 'P' && Math.floor(sq / 8) === 0) ||
        (movingPiece === 'p' && Math.floor(sq / 8) === 7)) {
      pendingPromoUci = uci;
      document.getElementById('promo-overlay').classList.add('show');
      // Update promo piece colours for current side
      const isW = movingPiece === 'P';
      document.querySelectorAll('.promo-piece').forEach((el, i) => {
        const wPieces = ['♛','♜','♝','♞'];
        const bPieces = ['♛','♜','♝','♞'];
        el.childNodes[0].textContent = isW ? ['♕','♖','♗','♘'][i] : bPieces[i];
      });
      return;
    }
    submitMove(uci);
  }
}

function submitPromotion(piece) {
  document.getElementById('promo-overlay').classList.remove('show');
  if (pendingPromoUci) submitMove(pendingPromoUci + piece);
  pendingPromoUci = null;
}

function submitMove(uci) {
  selectedSq = null; legalMoves = [];
  socket.emit('move_input', { uci });
  drawBoard();
}

// ── Pointer events (click + drag) ─────────────────────────────────────────
let pointerDown = false;
let pointerStartSq = null;
let hasDragged = false;

canvas.addEventListener('pointerdown', e => {
  e.preventDefault();
  const sq = getBoardSq(e.clientX, e.clientY);
  if (sq === null) return;
  const board = parseFen(currentFen);
  pointerDown    = true;
  pointerStartSq = sq;
  hasDragged     = false;
  // Start drag if a piece is here
  if (board[sq]) {
    dragPiece = { sq, piece: board[sq], x: e.clientX - canvas.getBoundingClientRect().left, y: e.clientY - canvas.getBoundingClientRect().top };
    selectedSq = sq;
    legalMoves = [];
    fetchLegalMoves(sq);
    drawBoard();
  }
});

canvas.addEventListener('pointermove', e => {
  if (!pointerDown || !dragPiece) return;
  const rect = canvas.getBoundingClientRect();
  dragPiece.x = e.clientX - rect.left;
  dragPiece.y = e.clientY - rect.top;
  hasDragged = true;
  drawBoard();
});

canvas.addEventListener('pointerup', e => {
  if (!pointerDown) return;
  pointerDown = false;
  const sq = getBoardSq(e.clientX, e.clientY);

  if (dragPiece && hasDragged) {
    // Drop
    if (sq !== null && sq !== dragPiece.sq) {
      const board = parseFen(currentFen);
      const movingPiece = board[dragPiece.sq];
      const uci = sqToAlg(dragPiece.sq) + sqToAlg(sq);
      dragPiece = null;
      if ((movingPiece === 'P' && Math.floor(sq / 8) === 0) ||
          (movingPiece === 'p' && Math.floor(sq / 8) === 7)) {
        pendingPromoUci = uci;
        selectedSq = null; legalMoves = [];
        document.getElementById('promo-overlay').classList.add('show');
      } else {
        submitMove(uci);
      }
    } else {
      dragPiece = null;
      drawBoard();
    }
  } else {
    // Click
    dragPiece = null;
    handleSquareClick(sq);
  }
});

canvas.addEventListener('touchstart',  e => e.preventDefault(), { passive: false });

// ── Socket ─────────────────────────────────────────────────────────────────
const socket = io();

socket.on('connect', () => {
  document.getElementById('conn-dot').classList.add('live');
  document.querySelector('.status-line').textContent = 'Connected';
});
socket.on('disconnect', () => {
  document.getElementById('conn-dot').classList.remove('live');
  document.querySelector('.status-line').textContent = 'Disconnected — reconnecting…';
});

socket.on('legal_moves', data => {
  legalMoves = data.moves || [];
  drawBoard();
});

socket.on('move_rejected', d => {
  showToast('Move rejected: ' + (d.reason || 'illegal'), 'err');
  selectedSq = null; legalMoves = []; dragPiece = null;
  drawBoard();
});

socket.on('move_accepted', () => {
  selectedSq = null; legalMoves = [];
});

socket.on('state_update', d => {
  currentFen = d.fen || currentFen;

  // Last move highlight
  lastMoveSqs = [];
  if (d.last_move && d.last_move.length >= 4) {
    lastMoveSqs = [algToSq(d.last_move.slice(0,2)), algToSq(d.last_move.slice(2,4))];
  }
  // Hint highlight
  hintSqs = [];
  if (d.hint_move && d.hint_move.length >= 4) {
    hintSqs = [algToSq(d.hint_move.slice(0,2)), algToSq(d.hint_move.slice(2,4))];
  }

  drawBoard();

  // Turn banner
  const banner = document.getElementById('turn-banner');
  const turnTxt = document.getElementById('turn-text');
  banner.className = 'turn-banner';
  if (d.thinking) {
    banner.classList.add('thinking');
    turnTxt.textContent = 'Engine thinking…';
  } else if (d.whose_turn === 'White') {
    banner.classList.add('white');
    turnTxt.textContent = "White's turn";
  } else {
    banner.classList.add('black');
    turnTxt.textContent = "Black's turn";
  }

  // Eval bar
  const score = Math.max(-500, Math.min(500, d.eval_score || 0));
  const pct   = Math.round((score + 500) / 1000 * 100);
  document.getElementById('eval-white').style.width = pct + '%';
  const absScore = (Math.abs(score) / 100).toFixed(2);
  const sign     = score > 0 ? '+' : score < 0 ? '' : '';
  document.getElementById('eval-w-label').textContent = score >= 0 ? sign + (score/100).toFixed(2) : '0.00';
  document.getElementById('eval-b-label').textContent = score <  0 ? (score/100).toFixed(2) : '0.00';

  // Status
  document.getElementById('status-line').textContent = d.status || '';

  // Stats
  document.getElementById('s-mode').textContent  = d.game_mode  || '—';
  document.getElementById('s-diff').textContent  = d.difficulty || '—';
  document.getElementById('s-moves').textContent = d.move_count || 0;
  document.getElementById('s-turn').textContent  = d.whose_turn || '—';
  document.getElementById('badge-mode').textContent = d.game_mode || '—';
  document.getElementById('badge-turn').textContent = (d.whose_turn || '—') + "'s turn";

  // Move history
  const hist  = d.move_history || [];
  const pairs = [];
  for (let i = 0; i < hist.length; i += 2) {
    pairs.push([Math.floor(i/2)+1, hist[i], hist[i+1] || '']);
  }
  const ml = document.getElementById('move-list');
  if (!pairs.length) {
    ml.innerHTML = '<span class="move-empty">No moves yet</span>';
  } else {
    ml.innerHTML = pairs.map((p, idx) =>
      `<div class="move-row${idx===pairs.length-1?' latest':''}">
        <span class="move-num">${p[0]}.</span>
        <span class="move-w">${p[1]}</span>
        <span class="move-b">${p[2]}</span>
      </div>`
    ).join('');
    ml.scrollTop = ml.scrollHeight;
  }

  // USB
  const usbBtn = document.getElementById('usb-btn');
  usbBtn.disabled = !d.usb_available;
  document.getElementById('usb-status').textContent = d.usb_available
    ? 'USB drive ready — click to save' : 'No USB drive detected';

  // Voice
  const vt = document.getElementById('voice-toggle');
  vt.checked = !!d.voice_enabled;
  document.getElementById('voice-label').textContent = d.voice_enabled
    ? 'Announcements on' : 'Announcements off';

  // Theme sync
  syncThemeGrid(d.theme);
});

// ── Legal moves request / response ────────────────────────────────────────
// Server-side: add this socketio handler to return legal moves for a square.
// We request via socket; the backend replies with legal_moves event.
// (Falls back to empty list — the move will be validated server-side anyway.)

// ── LED Theme swatches ─────────────────────────────────────────────────────
const THEMES = {
  classic: { label:'Classic', colors:['#fff','#000','#0f0','#0ff'] },
  fire:    { label:'Fire',    colors:['#ff8c00','#500a00','#ff0','#ff5000'] },
  ocean:   { label:'Ocean',   colors:['#0050b4','#00143c','#00ffc8','#64c8ff'] },
  forest:  { label:'Forest',  colors:['#145014','#050505','#b4e664','#64c864'] },
  neon:    { label:'Neon',    colors:['#7800c8','#000028','#00ff96','#ff00c8'] },
  custom:  { label:'Custom',  colors:['#888','#333','#fc0','#0cf'] },
};

function buildThemeGrid() {
  const grid = document.getElementById('theme-grid');
  grid.innerHTML = '';
  Object.entries(THEMES).forEach(([key, t]) => {
    const el = document.createElement('div');
    el.className = 'theme-swatch';
    el.dataset.key = key;
    el.style.background = `linear-gradient(135deg, ${t.colors[0]} 50%, ${t.colors[1]} 50%)`;
    el.innerHTML = `<span>${t.label}</span>`;
    el.addEventListener('click', () => selectTheme(key));
    grid.appendChild(el);
  });
}

function syncThemeGrid(active) {
  document.querySelectorAll('.theme-swatch').forEach(el => {
    el.classList.toggle('active', el.dataset.key === active);
  });
  document.getElementById('custom-panel').style.display =
    active === 'custom' ? 'block' : 'none';
}

function selectTheme(key) {
  if (key === 'custom') {
    document.getElementById('custom-panel').style.display = 'block';
    syncThemeGrid('custom');
    return;
  }
  document.getElementById('custom-panel').style.display = 'none';
  socket.emit('set_theme', { theme: key });
}

function liveCustomTheme() { /* previews colour in pickers immediately */ }

function applyCustom() {
  socket.emit('set_custom_theme', {
    light: document.getElementById('c-light').value,
    dark:  document.getElementById('c-dark').value,
    move:  document.getElementById('c-move').value,
    hint:  document.getElementById('c-hint').value,
  });
  showToast('Custom theme applied to LEDs');
}

// ── Action dispatch ────────────────────────────────────────────────────────
function doAction(name) {
  socket.emit(name);
  if (name === 'hint') showToast('Hint requested…');
  if (name === 'undo') showToast('Undo requested…');
  if (name === 'save_usb') showToast('Saving to USB…');
}

// ── System info polling ────────────────────────────────────────────────────
function pollSys() {
  fetch('/api/system').then(r => r.json()).then(d => {
    document.getElementById('sys-temp').textContent =
      d.cpu_temp_c !== 'N/A' ? d.cpu_temp_c + ' °C' : '—';
    document.getElementById('sys-cpu').textContent =
      d.cpu_pct    !== 'N/A' ? d.cpu_pct    + ' %'  : '—';
    document.getElementById('sys-ram').textContent =
      d.ram_used_mb !== 'N/A'
        ? d.ram_used_mb + ' / ' + d.ram_total_mb + ' MB' : '—';
  }).catch(() => {});
}
pollSys();
setInterval(pollSys, 6000);

// ── OTA update ─────────────────────────────────────────────────────────────
function otaUpdate() {
  const token = prompt('OTA token:');
  if (!token) return;
  const restart = confirm('Restart service after pulling?');
  const out = document.getElementById('ota-out');
  out.style.display = 'block';
  out.textContent   = 'Running git pull…\n';
  fetch('/api/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, restart }),
  }).then(r => r.json()).then(d => {
    out.textContent = d.error || d.output || 'Done.';
    if (d.restarting) out.textContent += '\nRestarting…';
  }).catch(e => { out.textContent = 'Error: ' + e; });
}

// ── Toast notifications ────────────────────────────────────────────────────
function showToast(msg, type = 'ok', ms = 3000) {
  const wrap  = document.getElementById('toast-wrap');
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = msg;
  wrap.appendChild(toast);
  requestAnimationFrame(() => requestAnimationFrame(() => toast.classList.add('show')));
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 300);
  }, ms);
}

// ── Resize handler ─────────────────────────────────────────────────────────
function onResize() { drawBoard(); }
window.addEventListener('resize', onResize);
new ResizeObserver(onResize).observe(document.getElementById('board-wrap'));

// ── Init ───────────────────────────────────────────────────────────────────
buildThemeGrid();
drawBoard();
</script>
</body>
</html>"""