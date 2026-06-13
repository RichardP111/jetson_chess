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


def start_server(host: str = None, port: int = None):
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
    log.info(f"Dashboard client connected: {request.sid}")
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


DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chess Board — Control</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Google+Sans:ital,wght@0,400;0,500;0,600;1,400&family=Google+Sans+Mono&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
/* ── Design tokens ── */
:root {
  --gold:       #c9a84c;
  --gold-dim:   #9a7a30;
  --gold-glow:  rgba(201,168,76,0.15);
  --font-mono:  'Google Sans Mono', 'Fira Mono', monospace;
  --font-sans:  'Google Sans', system-ui, sans-serif;
  --r:          8px;
  --r-lg:       14px;
  --t:          0.18s ease;
}
[data-theme="dark"] {
  --bg:       #111418; --surface: #191d23; --surface2: #20252d;
  --border:   rgba(255,255,255,0.08); --border-h: rgba(255,255,255,0.16);
  --text:     #e8e6e1; --text2: #9a9690; --text3: #5a5855;
  --bb:       rgba(255,255,255,0.06);
  --sq-l:     #b0956e; --sq-d: #6e4e2e;
}
[data-theme="light"] {
  --bg:       #f4f1ec; --surface: #ffffff; --surface2: #f0ede8;
  --border:   rgba(0,0,0,0.10); --border-h: rgba(0,0,0,0.22);
  --text:     #1a1713; --text2: #6b6460; --text3: #b0ada8;
  --bb:       rgba(0,0,0,0.05);
  --sq-l:     #f0d9b5; --sq-d: #b58863;
}

/* ── Reset ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:15px}
body{font-family:var(--font-sans);background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}

/* ── Topbar ── */
.topbar{display:flex;align-items:center;gap:10px;padding:0 20px;height:56px;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100}
.logo{font-size:1.1rem;font-weight:600;letter-spacing:-0.02em;color:var(--gold);display:flex;align-items:center;gap:8px;white-space:nowrap}
.logo svg{width:22px;height:22px;flex-shrink:0}
.spacer{flex:1}
.badge{font-size:0.72rem;font-weight:500;padding:3px 9px;border-radius:20px;background:var(--bb);border:1px solid var(--border);color:var(--text2);white-space:nowrap}
.badge.gold{background:var(--gold-glow);border-color:var(--gold-dim);color:var(--gold)}
.conn-dot{width:8px;height:8px;border-radius:50%;background:#dc4646;transition:background var(--t);flex-shrink:0}
.conn-dot.live{background:#50be64}
.tbtn{background:none;border:1px solid var(--border);border-radius:var(--r);padding:6px 11px;cursor:pointer;color:var(--text2);font-size:0.78rem;font-family:var(--font-sans);transition:all var(--t);display:flex;align-items:center;gap:5px}
.tbtn:hover{border-color:var(--border-h);color:var(--text)}
.tbtn.dev-unlocked{border-color:var(--gold-dim);color:var(--gold)}

/* ── Layout ── */
.layout{display:grid;grid-template-columns:1fr 336px;gap:16px;padding:16px;max-width:1120px;margin:0 auto}
@media(max-width:900px){.layout{grid-template-columns:1fr}}

/* ── Cards ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:16px}
.card-title{font-size:0.68rem;font-weight:600;letter-spacing:0.09em;text-transform:uppercase;color:var(--text3);margin-bottom:12px}

/* ── Board ── */
#board-wrap{position:relative;width:100%;max-width:520px;margin:0 auto}
#board-canvas{display:block;width:100%;aspect-ratio:1;border-radius:var(--r);cursor:pointer;touch-action:none}

/* ── Eval bar ── */
.eval-wrap{width:100%;margin-top:10px}
.eval-track{height:6px;border-radius:3px;background:var(--surface2);overflow:hidden;position:relative}
.eval-w{position:absolute;left:0;top:0;bottom:0;background:var(--sq-l);transition:width 0.5s cubic-bezier(.4,0,.2,1)}
.eval-labels{display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text3);font-family:var(--font-mono);margin-top:4px}

/* ── Turn banner ── */
.turn-banner{width:100%;padding:10px 14px;border-radius:var(--r);font-weight:600;font-size:0.88rem;display:flex;align-items:center;gap:8px;transition:all var(--t);margin-top:10px}
.turn-banner.white{background:rgba(240,217,181,0.15);border:1px solid rgba(240,217,181,0.30);color:var(--sq-l)}
.turn-banner.black{background:rgba(100,80,60,0.20);border:1px solid rgba(100,80,60,0.40);color:#b09070}
.turn-banner.thinking{background:var(--gold-glow);border:1px solid var(--gold-dim);color:var(--gold);animation:think-p 1.2s ease-in-out infinite}
@keyframes think-p{0%,100%{opacity:1}50%{opacity:0.6}}
.turn-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.turn-banner.white .turn-dot{background:#f0d9b5}
.turn-banner.black .turn-dot{background:#b09070}
.turn-banner.thinking .turn-dot{background:var(--gold);animation:think-d .6s ease-in-out infinite alternate}
@keyframes think-d{from{transform:scale(1)}to{transform:scale(1.6)}}

/* ── Status ── */
.status-line{font-size:0.80rem;color:var(--text2);padding:8px 12px;background:var(--surface2);border-radius:var(--r);margin-top:8px;min-height:34px;display:flex;align-items:center;font-family:var(--font-mono);border:1px solid var(--border)}

/* ── Right col ── */
.right-col{display:flex;flex-direction:column;gap:12px}

/* ── Stats ── */
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.stat-cell{background:var(--surface2);border-radius:var(--r);padding:10px 12px;border:1px solid var(--border)}
.stat-label{font-size:0.65rem;color:var(--text3);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px}
.stat-value{font-family:var(--font-mono);font-size:1.05rem;font-weight:600;color:var(--gold)}

/* ── Move history ── */
.move-history{max-height:200px;overflow-y:auto;font-family:var(--font-mono);font-size:0.78rem}
.move-history::-webkit-scrollbar{width:3px}
.move-history::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.move-row{display:grid;grid-template-columns:28px 1fr 1fr;gap:6px;padding:3px 0;border-bottom:1px solid var(--border);align-items:center}
.move-row:last-child{border-bottom:none}
.move-num{color:var(--text3);font-size:.70rem}
.move-w,.move-b{color:var(--text2)}
.move-row.latest .move-w,.move-row.latest .move-b{color:var(--gold);font-weight:600}
.move-empty{color:var(--text3);font-size:.78rem;font-style:italic}

/* ── Buttons ── */
.btn-row{display:flex;gap:8px;flex-wrap:wrap}
.btn{padding:8px 14px;border-radius:var(--r);border:1px solid var(--border);background:var(--surface2);color:var(--text);cursor:pointer;font-size:0.82rem;font-family:var(--font-sans);transition:all var(--t);display:flex;align-items:center;gap:6px;white-space:nowrap}
.btn:hover{border-color:var(--border-h);background:var(--surface)}
.btn:active{transform:scale(0.97)}
.btn:disabled{opacity:0.35;pointer-events:none}
.btn.primary{background:var(--gold-glow);border-color:var(--gold-dim);color:var(--gold);font-weight:600}
.btn.primary:hover{background:rgba(201,168,76,.22);border-color:var(--gold)}
.btn.sm{padding:5px 10px;font-size:0.75rem}
.btn-full{width:100%;justify-content:center}

/* ── Theme swatches ── */
.theme-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:6px;margin-bottom:10px}
.theme-sw{aspect-ratio:1;border-radius:6px;cursor:pointer;border:2px solid transparent;transition:all var(--t);display:flex;align-items:flex-end;justify-content:center;overflow:hidden;position:relative;padding-bottom:3px}
.theme-sw:hover{transform:scale(1.06)}
.theme-sw.active{border-color:var(--gold)}
.theme-sw span{font-size:0.52rem;font-weight:600;text-shadow:0 1px 3px rgba(0,0,0,.7);color:#fff;line-height:1;z-index:1}
.custom-theme-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.color-row{display:flex;align-items:center;gap:8px;font-size:0.75rem;color:var(--text2)}
.color-row input[type=color]{width:28px;height:28px;padding:2px;border-radius:4px;border:1px solid var(--border);background:var(--surface2);cursor:pointer;flex-shrink:0}

/* ── Sys info ── */
.sys-row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border);font-size:0.78rem}
.sys-row:last-child{border-bottom:none}
.sys-key{color:var(--text2)}
.sys-val{font-family:var(--font-mono);color:var(--gold);font-size:0.78rem}

/* ── Toggle ── */
.toggle-wrap{display:flex;align-items:center;gap:10px}
.toggle{position:relative;width:36px;height:20px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0;position:absolute}
.toggle-track{position:absolute;inset:0;background:var(--surface2);border:1px solid var(--border);border-radius:10px;cursor:pointer;transition:all var(--t)}
.toggle input:checked+.toggle-track{background:var(--gold-glow);border-color:var(--gold-dim)}
.toggle-thumb{position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--text3);transition:all var(--t);pointer-events:none}
.toggle input:checked~.toggle-thumb{transform:translateX(16px);background:var(--gold)}
.toggle-label{font-size:0.82rem;color:var(--text2)}

/* ── OTA ── */
.ota-out{display:none;background:#0d1117;border-radius:6px;padding:10px;font-family:var(--font-mono);font-size:.70rem;color:#58a65c;max-height:120px;overflow-y:auto;margin-top:8px;border:1px solid rgba(88,166,92,.2);white-space:pre-wrap}
.divider{height:1px;background:var(--border);margin:10px 0}

/* ── Shared overlay ── */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.74);z-index:200;align-items:center;justify-content:center;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);padding:16px}
.overlay.show{display:flex}
.modal-close{position:absolute;top:14px;right:16px;background:none;border:none;cursor:pointer;color:var(--text3);font-size:1rem;line-height:1;padding:4px 6px;border-radius:4px;transition:color var(--t);z-index:1}
.modal-close:hover{color:var(--text)}

/* ── Promo modal ── */
.promo-modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:24px;text-align:center;width:100%;max-width:340px;position:relative}
.promo-modal h3{font-size:.9rem;color:var(--text2);margin-bottom:16px;font-weight:500}
.promo-pieces{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.promo-piece{aspect-ratio:1;border-radius:var(--r);background:var(--surface2);border:1px solid var(--border);cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;font-size:2.2rem;transition:all var(--t)}
.promo-piece span{font-size:.62rem;color:var(--text3);font-family:var(--font-mono)}
.promo-piece:hover{border-color:var(--gold-dim);background:var(--gold-glow)}

/* ── PIN modal ── */
.pin-modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:28px 24px 20px;text-align:center;width:100%;max-width:290px}
.pin-modal h3{font-size:1rem;font-weight:600;color:var(--text);margin-bottom:4px;display:flex;align-items:center;justify-content:center;gap:8px}
.pin-modal h3 svg{color:var(--gold)}
.pin-sub{font-size:.78rem;color:var(--text3);margin-bottom:20px}
.pin-dots{display:flex;gap:12px;justify-content:center;margin-bottom:22px}
.pd{width:14px;height:14px;border-radius:50%;border:2px solid var(--border);transition:all var(--t)}
.pd.filled{background:var(--gold);border-color:var(--gold)}
.pd.err{background:#dc4646;border-color:#dc4646;animation:pshake .3s ease}
@keyframes pshake{0%,100%{transform:translateX(0)}25%{transform:translateX(-4px)}75%{transform:translateX(4px)}}
.pin-numpad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.pin-key{height:48px;border-radius:var(--r);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-size:1.1rem;font-family:var(--font-mono);font-weight:500;cursor:pointer;transition:all var(--t);display:flex;align-items:center;justify-content:center;user-select:none}
.pin-key:hover{background:var(--surface);border-color:var(--border-h)}
.pin-key:active{transform:scale(0.93)}
.pin-key.del{color:var(--text3);font-size:.9rem}
.pin-key.zero{grid-column:2}
.pin-err{font-size:.75rem;color:#dc7070;margin-top:12px;min-height:18px;font-family:var(--font-mono)}

/* ── Dev modal ── */
.dev-modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);width:100%;max-width:560px;max-height:88vh;display:flex;flex-direction:column;overflow:hidden;position:relative}
.dev-head{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--border);flex-shrink:0}
.dev-head h2{font-size:.95rem;font-weight:600;color:var(--text);display:flex;align-items:center;gap:8px}
.dev-badge{font-size:.62rem;font-weight:700;letter-spacing:.1em;padding:2px 7px;border-radius:4px;background:rgba(220,100,50,.15);border:1px solid rgba(220,100,50,.4);color:#dc6432;text-transform:uppercase}
.dev-body{overflow-y:auto;padding:16px 20px;flex:1}
.dev-body::-webkit-scrollbar{width:3px}
.dev-body::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.dev-section{margin-bottom:20px}
.dev-sec-title{font-size:.66rem;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--gold);margin-bottom:10px;display:flex;align-items:center;gap:6px}
.dev-sec-title::after{content:'';flex:1;height:1px;background:var(--border)}
.dev-field{display:grid;grid-template-columns:1fr 130px;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border)}
.dev-field:last-child{border-bottom:none}
.dev-lbl{font-size:.80rem;color:var(--text2);line-height:1.3}
.dev-lbl small{display:block;font-size:.66rem;color:var(--text3);font-family:var(--font-mono);margin-top:1px}
.dev-input{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:var(--r);padding:6px 9px;color:var(--text);font-size:.80rem;font-family:var(--font-mono);text-align:right;transition:border-color var(--t)}
.dev-input:focus{outline:none;border-color:var(--gold-dim)}
.dev-input.changed{border-color:var(--gold);color:var(--gold)}
.dev-foot{padding:12px 20px;border-top:1px solid var(--border);display:flex;gap:8px;justify-content:flex-end;flex-shrink:0;background:var(--surface)}
.dev-note{font-size:.70rem;color:var(--text3);font-family:var(--font-mono);padding:8px 12px;background:var(--surface2);border-radius:var(--r);border:1px solid var(--border);margin-bottom:14px;line-height:1.6}
.dev-note strong{color:var(--gold);font-weight:500}

/* ── About modal ── */
.about-modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:32px 28px 28px;width:100%;max-width:330px;text-align:center;position:relative}
.about-icon{width:58px;height:58px;background:var(--gold-glow);border:1px solid var(--gold-dim);border-radius:16px;display:flex;align-items:center;justify-content:center;margin:0 auto 14px;font-size:1.9rem}
.about-name{font-size:1.1rem;font-weight:600;color:var(--text);margin-bottom:5px}
.about-ver{font-size:.75rem;font-family:var(--font-mono);color:var(--gold);background:var(--gold-glow);border:1px solid var(--gold-dim);border-radius:20px;padding:2px 10px;display:inline-block;margin-bottom:16px}
.about-desc{font-size:.82rem;color:var(--text2);line-height:1.65;margin-bottom:20px}
.about-div{height:1px;background:var(--border);margin:16px 0}
.about-credit{font-size:.82rem;color:var(--text2);display:flex;align-items:center;justify-content:center;gap:5px;flex-wrap:wrap}
.heart{color:#e84040;animation:hb 1.6s ease-in-out infinite;display:inline-block}
@keyframes hb{0%,100%{transform:scale(1)}14%{transform:scale(1.28)}28%{transform:scale(1)}42%{transform:scale(1.18)}56%{transform:scale(1)}}
.about-chips{display:flex;gap:6px;justify-content:center;margin-top:14px;flex-wrap:wrap}
.chip{font-size:.66rem;color:var(--text3);background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:3px 9px;font-family:var(--font-mono)}

/* ── Toasts ── */
.toast-wrap{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:8px;z-index:400;pointer-events:none}
.toast{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:10px 16px;font-size:.82rem;color:var(--text);opacity:0;transform:translateY(8px);transition:all .25s ease;pointer-events:auto;max-width:280px;font-family:var(--font-sans)}
.toast.show{opacity:1;transform:none}
.toast.ok{border-color:rgba(80,190,100,.4);background:rgba(80,190,100,.08)}
.toast.err{border-color:rgba(220,70,70,.4);background:rgba(220,70,70,.08);color:#e07070}
.toast.dev{border-color:rgba(220,100,50,.4);background:rgba(220,100,50,.08);color:#dc8050}

/* ── Responsive ── */
@media(max-width:600px){
  .topbar{padding:0 12px;gap:6px}
  .badge:not(:last-of-type):not(.gold){display:none}
  .layout{padding:10px;gap:10px}
  .card{padding:12px}
  .dev-field{grid-template-columns:1fr 100px}
  .theme-grid{grid-template-columns:repeat(3,1fr)}
}
</style>
</head>
<body>

<!-- ═══ MODALS ═══ -->

<div class="overlay" id="ov-promo">
  <div class="promo-modal">
    <button class="modal-close" onclick="closeOv('ov-promo')">✕</button>
    <h3>Promote pawn to…</h3>
    <div class="promo-pieces">
      <div class="promo-piece" onclick="submitPromo('q')">♛<span>Queen</span></div>
      <div class="promo-piece" onclick="submitPromo('r')">♜<span>Rook</span></div>
      <div class="promo-piece" onclick="submitPromo('b')">♝<span>Bishop</span></div>
      <div class="promo-piece" onclick="submitPromo('n')">♞<span>Knight</span></div>
    </div>
  </div>
</div>

<div class="overlay" id="ov-pin">
  <div class="pin-modal">
    <h3>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      Developer access
    </h3>
    <p class="pin-sub">Enter your 4-digit PIN</p>
    <div class="pin-dots">
      <div class="pd" id="pd0"></div><div class="pd" id="pd1"></div>
      <div class="pd" id="pd2"></div><div class="pd" id="pd3"></div>
    </div>
    <div class="pin-numpad">
      <div class="pin-key" onclick="pk('1')">1</div>
      <div class="pin-key" onclick="pk('2')">2</div>
      <div class="pin-key" onclick="pk('3')">3</div>
      <div class="pin-key" onclick="pk('4')">4</div>
      <div class="pin-key" onclick="pk('5')">5</div>
      <div class="pin-key" onclick="pk('6')">6</div>
      <div class="pin-key" onclick="pk('7')">7</div>
      <div class="pin-key" onclick="pk('8')">8</div>
      <div class="pin-key" onclick="pk('9')">9</div>
      <div class="pin-key del" onclick="pdel()">⌫</div>
      <div class="pin-key zero" onclick="pk('0')">0</div>
      <div class="pin-key del" onclick="closeOv('ov-pin')">✕</div>
    </div>
    <div class="pin-err" id="pin-err"></div>
  </div>
</div>

<div class="overlay" id="ov-dev">
  <div class="dev-modal">
    <button class="modal-close" onclick="closeOv('ov-dev')">✕</button>
    <div class="dev-head">
      <h2>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
        Dev settings
        <span class="dev-badge">session only</span>
      </h2>
    </div>
    <div class="dev-body">
      <div class="dev-note"><strong>⚠ Session-only.</strong> Changes apply immediately but are <strong>not</strong> written to .env — a restart reverts them. Edit config.py or .env for permanent changes.</div>

      <div class="dev-section">
        <div class="dev-sec-title">Stockfish / AI</div>
        <div class="dev-field"><div class="dev-lbl">Default difficulty<small>1–20</small></div><input class="dev-input" id="dv-difficulty_default" type="number" min="1" max="20" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Min difficulty<small>selection lower bound</small></div><input class="dev-input" id="dv-difficulty_min" type="number" min="1" max="20" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Max difficulty<small>selection upper bound</small></div><input class="dev-input" id="dv-difficulty_max" type="number" min="1" max="20" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Default move time<small>ms</small></div><input class="dev-input" id="dv-movetime_default_ms" type="number" min="500" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Min move time<small>ms</small></div><input class="dev-input" id="dv-movetime_min_ms" type="number" min="500" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Max move time<small>ms</small></div><input class="dev-input" id="dv-movetime_max_ms" type="number" min="500" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Stockfish path<small>full binary path</small></div><input class="dev-input" id="dv-stockfish_path" type="text" style="text-align:left;font-size:.70rem" oninput="markC(this)"></div>
      </div>

      <div class="dev-section">
        <div class="dev-sec-title">LED hardware</div>
        <div class="dev-field"><div class="dev-lbl">Board brightness cap<small>0–255 (76 ≈ 30%)</small></div><input class="dev-input" id="dv-chess_led_brightness" type="number" min="0" max="255" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Startup sweep delay<small>seconds per LED</small></div><input class="dev-input" id="dv-startup_led_delay_s" type="number" min="0" step="0.005" oninput="markC(this)"></div>
      </div>

      <div class="dev-section">
        <div class="dev-sec-title">Input &amp; UX timings</div>
        <div class="dev-field"><div class="dev-lbl">Button debounce<small>seconds</small></div><input class="dev-input" id="dv-button_debounce_s" type="number" min="0.05" step="0.01" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Hint auto-dismiss<small>seconds</small></div><input class="dev-input" id="dv-hint_dismiss_s" type="number" min="1" step="0.5" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Undo stack depth<small>max half-moves</small></div><input class="dev-input" id="dv-undo_max_half_moves" type="number" min="2" max="40" oninput="markC(this)"></div>
      </div>

      <div class="dev-section">
        <div class="dev-sec-title">Voice / TTS</div>
        <div class="dev-field"><div class="dev-lbl">Speech rate<small>words per minute</small></div><input class="dev-input" id="dv-tts_rate" type="number" min="80" max="300" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Volume<small>0.0 – 1.0</small></div><input class="dev-input" id="dv-tts_volume" type="number" min="0" max="1" step="0.05" oninput="markC(this)"></div>
      </div>

      <div class="dev-section">
        <div class="dev-sec-title">Storage &amp; network</div>
        <div class="dev-field"><div class="dev-lbl">PGN folder on USB<small>subfolder name</small></div><input class="dev-input" id="dv-pgn_subdir" type="text" style="text-align:left" oninput="markC(this)"></div>
        <div class="dev-field"><div class="dev-lbl">Dashboard port<small>restart required</small></div><input class="dev-input" id="dv-web_port" type="number" min="1024" max="65535" oninput="markC(this)"></div>
      </div>
    </div>
    <div class="dev-foot">
      <button class="btn sm" onclick="closeOv('ov-dev')">Cancel</button>
      <button class="btn sm primary" onclick="saveDevSettings()">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
        Apply to session
      </button>
    </div>
  </div>
</div>

<div class="overlay" id="ov-about">
  <div class="about-modal">
    <button class="modal-close" onclick="closeOv('ov-about')">✕</button>
    <div class="about-icon">♟</div>
    <div class="about-name">Smart Chess Board</div>
    <div class="about-ver">v4.0 — Jetson Orin Nano</div>
    <p class="about-desc">A fully self-contained AI chess board with Stockfish engine, Lichess online play, local two-player mode, voice announcements, and real-time WS2812b LED feedback.</p>
    <div class="about-div"></div>
    <div class="about-credit">
      Made with <span class="heart">❤️</span> by <strong>Richard P</strong>
    </div>
    <div class="about-chips">
      <span class="chip">Python 3.12</span>
      <span class="chip">Stockfish</span>
      <span class="chip">Flask · SocketIO</span>
      <span class="chip">WS2812b</span>
      <span class="chip">espeak-ng</span>
      <span class="chip">Jetson Orin</span>
    </div>
  </div>
</div>

<div class="toast-wrap" id="toast-wrap"></div>

<!-- ═══ TOPBAR ═══ -->
<header class="topbar">
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M9 2h6l-1 4H10L9 2z"/><path d="M10 6c0 2-2 3-2 5h8c0-2-2-3-2-5"/>
      <rect x="8" y="11" width="8" height="2" rx="1"/><path d="M7 13v3l-1 3h10l-1-3v-3"/>
    </svg>
    Smart Chess
  </div>
  <div class="spacer"></div>
  <div style="display:flex;align-items:center;gap:8px">
    <span class="badge" id="badge-mode">—</span>
    <span class="badge" id="badge-turn">—</span>
    <div class="conn-dot" id="conn-dot"></div>
  </div>
  <button class="tbtn" onclick="openAbout()" title="About">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
    <span style="font-size:.72rem">v4.0</span>
  </button>
  <button class="tbtn" id="dev-btn" onclick="openDev()" title="Developer settings">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
    Dev
  </button>
  <button class="tbtn" onclick="toggleTheme()" title="Toggle light/dark">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
      <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
    </svg>
  </button>
</header>

<!-- ═══ LAYOUT ═══ -->
<main class="layout">

  <div style="display:flex;flex-direction:column;gap:12px">
    <div class="card">
      <div class="card-title">Board</div>
      <div id="board-wrap"><canvas id="board-canvas"></canvas></div>
      <div class="eval-wrap">
        <div class="eval-track"><div class="eval-w" id="eval-w" style="width:50%"></div></div>
        <div class="eval-labels"><span id="evl-w">0.00</span><span style="color:var(--text3)">evaluation</span><span id="evl-b">0.00</span></div>
      </div>
      <div class="turn-banner white" id="turn-banner"><div class="turn-dot"></div><span id="turn-txt">White's turn</span></div>
      <div class="status-line" id="status-line">Waiting for board…</div>
    </div>
    <div class="card">
      <div class="card-title">Controls</div>
      <div class="btn-row">
        <button class="btn primary" onclick="act('new_game')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4"/></svg>New game</button>
        <button class="btn" onclick="act('hint')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>Hint</button>
        <button class="btn" onclick="act('undo')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/></svg>Undo</button>
      </div>
    </div>
  </div>

  <div class="right-col">
    <div class="card">
      <div class="card-title">Session</div>
      <div class="stats-grid">
        <div class="stat-cell"><div class="stat-label">Mode</div><div class="stat-value" id="s-mode">—</div></div>
        <div class="stat-cell"><div class="stat-label">Difficulty</div><div class="stat-value" id="s-diff">—</div></div>
        <div class="stat-cell"><div class="stat-label">Moves</div><div class="stat-value" id="s-moves">0</div></div>
        <div class="stat-cell"><div class="stat-label">Turn</div><div class="stat-value" id="s-turn">—</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Move history</div>
      <div class="move-history" id="move-list"><span class="move-empty">No moves yet</span></div>
    </div>
    <div class="card">
      <div class="card-title">LED theme</div>
      <div class="theme-grid" id="theme-grid"></div>
      <div id="custom-panel" style="display:none">
        <div class="divider"></div>
        <div class="custom-theme-grid">
          <div class="color-row"><input type="color" id="c-light" value="#ffffff" oninput="liveTheme()"> Light sq.</div>
          <div class="color-row"><input type="color" id="c-dark"  value="#000000" oninput="liveTheme()"> Dark sq.</div>
          <div class="color-row"><input type="color" id="c-move"  value="#00ff00" oninput="liveTheme()"> Move</div>
          <div class="color-row"><input type="color" id="c-hint"  value="#00ffff" oninput="liveTheme()"> Hint</div>
        </div>
        <button class="btn btn-full" style="margin-top:8px" onclick="applyCustom()">Apply to LEDs</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Save &amp; export</div>
      <div class="btn-row">
        <button class="btn" id="usb-btn" onclick="act('save_usb')" disabled>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
          Save to USB
        </button>
        <a href="/api/pgn/download" style="text-decoration:none"><button class="btn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>PGN</button></a>
      </div>
      <div style="font-size:.72rem;color:var(--text3);margin-top:6px" id="usb-status">No USB drive detected</div>
    </div>
    <div class="card">
      <div class="card-title">Voice</div>
      <div class="toggle-wrap">
        <label class="toggle"><input type="checkbox" id="voice-toggle" onchange="act('toggle_voice')"><div class="toggle-track"></div><div class="toggle-thumb"></div></label>
        <span class="toggle-label" id="voice-label">Announcements off</span>
      </div>
      <div style="font-size:.70rem;color:var(--text3);margin-top:6px">Plug in a USB speaker to enable.</div>
    </div>
    <div class="card">
      <div class="card-title">Jetson system</div>
      <div class="sys-row"><span class="sys-key">CPU temp</span><span class="sys-val" id="sys-temp">—</span></div>
      <div class="sys-row"><span class="sys-key">CPU load</span><span class="sys-val" id="sys-cpu">—</span></div>
      <div class="sys-row"><span class="sys-key">RAM used</span><span class="sys-val" id="sys-ram">—</span></div>
      <div class="divider"></div>
      <button class="btn btn-full sm" onclick="otaUpdate()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4"/></svg>Check for updates</button>
      <div class="ota-out" id="ota-out"></div>
    </div>
  </div>
</main>

<script>
// ═══ THEME ═══
const PREF = matchMedia('(prefers-color-scheme:dark)').matches;
let CUR_THEME = localStorage.getItem('chess-theme') || (PREF?'dark':'light');
document.documentElement.setAttribute('data-theme', CUR_THEME);
function toggleTheme(){
  CUR_THEME = CUR_THEME==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',CUR_THEME);
  localStorage.setItem('chess-theme',CUR_THEME);
  drawBoard();
}

// ═══ BOARD CANVAS ═══
const CV = document.getElementById('board-canvas');
const CX = CV.getContext('2d');
let FEN='rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
let LEGAL=[],SEL=null,DRAG=null,LAST_SQS=[],HINT_SQS=[],PROMO_UCI=null;

const PC={'K':'♔','Q':'♕','R':'♖','B':'♗','N':'♘','P':'♙','k':'♚','q':'♛','r':'♜','b':'♝','n':'♞','p':'♟'};

function parseFen(fen){
  const b=new Array(64).fill(null);
  fen.split(' ')[0].split('/').forEach((row,r)=>{let f=0;for(const c of row){if(c>='1'&&c<='8')f+=+c;else{b[r*8+f]=c;f++;}}});
  return b;
}
function s2a(sq){'abcdefgh'[sq%8]+(8-Math.floor(sq/8));return 'abcdefgh'[sq%8]+(8-Math.floor(sq/8));}
function a2s(a){return(8-+a[1])*8+'abcdefgh'.indexOf(a[0]);}

function getC(){
  const dk=document.documentElement.getAttribute('data-theme')==='dark';
  return{
    l:dk?'#b0956e':'#f0d9b5', d:dk?'#6e4e2e':'#b58863',
    sel:'rgba(201,168,76,.72)', dot:'rgba(201,168,76,.32)', cap:'rgba(201,168,76,.58)',
    last:dk?'rgba(90,150,70,.50)':'rgba(120,190,90,.45)',
    hint:dk?'rgba(50,140,210,.55)':'rgba(60,160,220,.50)',
    crd:dk?'rgba(255,255,255,.22)':'rgba(0,0,0,.22)'
  };
}

function drawBoard(){
  const DPR=window.devicePixelRatio||1;
  const SZ=CV.parentElement.clientWidth;
  CV.width=SZ*DPR; CV.height=SZ*DPR;
  CV.style.width=SZ+'px'; CV.style.height=SZ+'px';
  CX.scale(DPR,DPR);
  const SQ=SZ/8, C=getC(), BD=parseFen(FEN);
  const LT=new Set(), LC=new Set();
  LEGAL.forEach(u=>{const t=a2s(u.slice(2,4));LT.add(t);if(BD[t])LC.add(t);});
  for(let i=0;i<64;i++){
    const r=Math.floor(i/8),f=i%8,x=f*SQ,y=r*SQ,il=(r+f)%2===0;
    CX.fillStyle=il?C.l:C.d; CX.fillRect(x,y,SQ,SQ);
    if(LAST_SQS.includes(i)){CX.fillStyle=C.last;CX.fillRect(x,y,SQ,SQ);}
    if(HINT_SQS.includes(i)){CX.fillStyle=C.hint;CX.fillRect(x,y,SQ,SQ);}
    if(SEL===i){CX.fillStyle=C.sel;CX.fillRect(x,y,SQ,SQ);}
    if(LT.has(i)){
      if(LC.has(i)){CX.strokeStyle=C.cap;CX.lineWidth=SQ*.08;CX.beginPath();CX.arc(x+SQ/2,y+SQ/2,SQ*.45,0,Math.PI*2);CX.stroke();}
      else{CX.fillStyle=C.dot;CX.beginPath();CX.arc(x+SQ/2,y+SQ/2,SQ*.16,0,Math.PI*2);CX.fill();}
    }
    CX.font=`${Math.round(SQ*.19)}px Google Sans Mono,monospace`;
    CX.fillStyle=C.crd; CX.textBaseline='top';
    if(f===0){CX.textAlign='left';CX.fillText(8-r,x+3,y+2);}
    if(r===7){CX.textAlign='right';CX.fillText('abcdefgh'[f],x+SQ-3,y+SQ-Math.round(SQ*.22));}
    const pc=BD[i];
    if(pc&&!(DRAG&&DRAG.sq===i)) drawPc(pc,x+SQ/2,y+SQ/2,SQ);
  }
  if(DRAG) drawPc(DRAG.pc,DRAG.x,DRAG.y,SQ*1.18);
}

function drawPc(p,cx,cy,sz){
  const w=p===p.toUpperCase();
  CX.font=`${Math.round(sz*.72)}px serif`;
  CX.textAlign='center'; CX.textBaseline='middle';
  CX.shadowColor=w?'rgba(0,0,0,.6)':'rgba(0,0,0,.4)'; CX.shadowBlur=sz*.06;
  CX.fillStyle=w?'#fffaf2':'#1a1008';
  CX.fillText(PC[p]||p,cx,cy+sz*.02); CX.shadowBlur=0;
}

function getSq(ex,ey){
  const R=CV.getBoundingClientRect(),SQ=R.width/8;
  const f=Math.floor((ex-R.left)/SQ),r=Math.floor((ey-R.top)/SQ);
  return(f<0||f>7||r<0||r>7)?null:r*8+f;
}

function fetchLegal(sq){socket.emit('get_legal_moves',{fen:FEN,sq:s2a(sq)});}

function clickSq(sq){
  if(sq===null)return;
  const bd=parseFen(FEN),pc=bd[sq];
  if(SEL===null){if(pc){SEL=sq;LEGAL=[];fetchLegal(sq);drawBoard();}}
  else if(SEL===sq){SEL=null;LEGAL=[];drawBoard();}
  else{tryMove(SEL,sq);}
}

function tryMove(from,to){
  const bd=parseFen(FEN),mp=bd[from];
  const uci=s2a(from)+s2a(to);
  SEL=null;LEGAL=[];
  if((mp==='P'&&Math.floor(to/8)===0)||(mp==='p'&&Math.floor(to/8)===7)){
    PROMO_UCI=uci;document.getElementById('ov-promo').classList.add('show');
  } else {socket.emit('move_input',{uci});drawBoard();}
}

function submitPromo(p){
  closeOv('ov-promo');
  if(PROMO_UCI)socket.emit('move_input',{uci:PROMO_UCI+p});
  PROMO_UCI=null;
}

let PD=false,HD=false;
CV.addEventListener('pointerdown',e=>{
  e.preventDefault();
  const sq=getSq(e.clientX,e.clientY);
  if(sq===null)return;
  const bd=parseFen(FEN);
  PD=true;HD=false;
  if(bd[sq]){
    const R=CV.getBoundingClientRect();
    DRAG={sq,pc:bd[sq],x:e.clientX-R.left,y:e.clientY-R.top};
    SEL=sq;LEGAL=[];fetchLegal(sq);drawBoard();
  }
});
CV.addEventListener('pointermove',e=>{
  if(!PD||!DRAG)return;
  const R=CV.getBoundingClientRect();
  DRAG.x=e.clientX-R.left;DRAG.y=e.clientY-R.top;HD=true;drawBoard();
});
CV.addEventListener('pointerup',e=>{
  if(!PD)return; PD=false;
  const sq=getSq(e.clientX,e.clientY);
  if(DRAG&&HD){
    if(sq!==null&&sq!==DRAG.sq){const from=DRAG.sq;DRAG=null;tryMove(from,sq);}
    else{DRAG=null;drawBoard();}
  } else {DRAG=null;clickSq(sq);}
});
CV.addEventListener('touchstart',e=>e.preventDefault(),{passive:false});

// ═══ SOCKET ═══
const socket=io();
socket.on('connect',()=>{document.getElementById('conn-dot').classList.add('live');document.getElementById('status-line').textContent='Connected';});
socket.on('disconnect',()=>{document.getElementById('conn-dot').classList.remove('live');document.getElementById('status-line').textContent='Disconnected — reconnecting…';});
socket.on('legal_moves',d=>{LEGAL=d.moves||[];drawBoard();});
socket.on('move_rejected',d=>{toast('Move rejected: '+(d.reason||'illegal'),'err');SEL=null;LEGAL=[];DRAG=null;drawBoard();});
socket.on('move_accepted',()=>{SEL=null;LEGAL=[];});

socket.on('state_update',d=>{
  FEN=d.fen||FEN;
  LAST_SQS=d.last_move&&d.last_move.length>=4?[a2s(d.last_move.slice(0,2)),a2s(d.last_move.slice(2,4))]:[];
  HINT_SQS=d.hint_move&&d.hint_move.length>=4?[a2s(d.hint_move.slice(0,2)),a2s(d.hint_move.slice(2,4))]:[];
  drawBoard();
  // Turn banner
  const bn=document.getElementById('turn-banner'),tt=document.getElementById('turn-txt');
  bn.className='turn-banner';
  if(d.thinking){bn.classList.add('thinking');tt.textContent='Engine thinking…';}
  else if(d.whose_turn==='White'){bn.classList.add('white');tt.textContent="White's turn";}
  else{bn.classList.add('black');tt.textContent="Black's turn";}
  // Eval
  const sc=Math.max(-500,Math.min(500,d.eval_score||0));
  document.getElementById('eval-w').style.width=Math.round((sc+500)/1000*100)+'%';
  document.getElementById('evl-w').textContent=sc>=0?'+'+(sc/100).toFixed(2):'0.00';
  document.getElementById('evl-b').textContent=sc<0?(sc/100).toFixed(2):'0.00';
  document.getElementById('status-line').textContent=d.status||'';
  document.getElementById('s-mode').textContent=d.game_mode||'—';
  document.getElementById('s-diff').textContent=d.difficulty||'—';
  document.getElementById('s-moves').textContent=d.move_count||0;
  document.getElementById('s-turn').textContent=d.whose_turn||'—';
  document.getElementById('badge-mode').textContent=d.game_mode||'—';
  document.getElementById('badge-turn').textContent=(d.whose_turn||'—')+"'s turn";
  // Move list
  const hist=d.move_history||[],pairs=[];
  for(let i=0;i<hist.length;i+=2)pairs.push([Math.floor(i/2)+1,hist[i],hist[i+1]||'']);
  const ml=document.getElementById('move-list');
  ml.innerHTML=!pairs.length?'<span class="move-empty">No moves yet</span>':pairs.map((p,idx)=>
    `<div class="move-row${idx===pairs.length-1?' latest':''}"><span class="move-num">${p[0]}.</span><span class="move-w">${p[1]}</span><span class="move-b">${p[2]}</span></div>`).join('');
  if(pairs.length)ml.scrollTop=ml.scrollHeight;
  // USB / voice
  document.getElementById('usb-btn').disabled=!d.usb_available;
  document.getElementById('usb-status').textContent=d.usb_available?'USB ready — click to save':'No USB drive detected';
  document.getElementById('voice-toggle').checked=!!d.voice_enabled;
  document.getElementById('voice-label').textContent=d.voice_enabled?'Announcements on':'Announcements off';
  syncThemes(d.theme);
});

// ═══ PIN & DEV ═══
const PIN_KEY='chess_dev_pin', DEF_PIN='1337';
let pinBuf='',devUnlocked=false;

function openDev(){devUnlocked?showDevPanel():(pinBuf='',updPinDots(),document.getElementById('pin-err').textContent='',document.getElementById('ov-pin').classList.add('show'));}
function pk(d){if(pinBuf.length>=4)return;pinBuf+=d;updPinDots();if(pinBuf.length===4)checkPin();}
function pdel(){pinBuf=pinBuf.slice(0,-1);updPinDots();document.getElementById('pin-err').textContent='';}
function updPinDots(){for(let i=0;i<4;i++){const e=document.getElementById('pd'+i);e.className='pd'+(i<pinBuf.length?' filled':'');}}
function checkPin(){
  const stored=localStorage.getItem(PIN_KEY)||DEF_PIN;
  if(pinBuf===stored){
    devUnlocked=true;closeOv('ov-pin');showDevPanel();
    const db=document.getElementById('dev-btn');db.classList.add('dev-unlocked');
  } else {
    for(let i=0;i<4;i++)document.getElementById('pd'+i).className='pd err';
    document.getElementById('pin-err').textContent='Incorrect PIN';
    setTimeout(()=>{pinBuf='';updPinDots();},800);
  }
}
function showDevPanel(){socket.emit('get_dev_settings');document.getElementById('ov-dev').classList.add('show');}

socket.on('dev_settings',data=>{
  Object.entries(data).forEach(([k,v])=>{const el=document.getElementById('dv-'+k);if(el){el.value=v;el.classList.remove('changed');}});
});
socket.on('dev_settings_saved',data=>{
  if(data.ok){toast('Applied ('+data.changed.length+' changed)','dev');document.querySelectorAll('.dev-input.changed').forEach(e=>e.classList.remove('changed'));}
});
function markC(el){el.classList.add('changed');}
function saveDevSettings(){
  const payload={};
  document.querySelectorAll('.dev-input').forEach(el=>{
    const k=el.id.replace('dv-','');
    payload[k]=el.type==='number'?parseFloat(el.value):el.value;
  });
  socket.emit('apply_dev_settings',payload);
}

// ═══ ABOUT ═══
function openAbout(){document.getElementById('ov-about').classList.add('show');}

// ═══ OVERLAY HELPERS ═══
function closeOv(id){document.getElementById(id).classList.remove('show');}
document.querySelectorAll('.overlay').forEach(el=>el.addEventListener('click',e=>{if(e.target===el)closeOv(el.id);}));
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelectorAll('.overlay.show').forEach(el=>closeOv(el.id));});

// ═══ LED THEMES ═══
const THEMES={
  classic:{label:'Classic',c:['#fff','#000','#0f0','#0ff']},
  fire:   {label:'Fire',   c:['#ff8c00','#500a00','#ff0','#f50']},
  ocean:  {label:'Ocean',  c:['#0050b4','#00143c','#0fc8','#64c8ff']},
  forest: {label:'Forest', c:['#145014','#050505','#b4e664','#64c864']},
  neon:   {label:'Neon',   c:['#7800c8','#000028','#00ff96','#f0c']},
  custom: {label:'Custom', c:['#888','#333','#fc0','#0cf']},
};
function buildThemes(){
  const g=document.getElementById('theme-grid');g.innerHTML='';
  Object.entries(THEMES).forEach(([k,t])=>{
    const el=document.createElement('div');el.className='theme-sw';el.dataset.key=k;
    el.style.background=`linear-gradient(135deg,${t.c[0]} 50%,${t.c[1]} 50%)`;
    el.innerHTML=`<span>${t.label}</span>`;
    el.addEventListener('click',()=>selTheme(k));g.appendChild(el);
  });
}
function syncThemes(a){
  document.querySelectorAll('.theme-sw').forEach(el=>el.classList.toggle('active',el.dataset.key===a));
  document.getElementById('custom-panel').style.display=a==='custom'?'block':'none';
}
function selTheme(k){
  if(k==='custom'){document.getElementById('custom-panel').style.display='block';syncThemes('custom');return;}
  document.getElementById('custom-panel').style.display='none';
  socket.emit('set_theme',{theme:k});
}
function liveTheme(){}
function applyCustom(){
  socket.emit('set_custom_theme',{light:document.getElementById('c-light').value,dark:document.getElementById('c-dark').value,move:document.getElementById('c-move').value,hint:document.getElementById('c-hint').value});
  toast('Custom theme applied to LEDs');
}

// ═══ ACTIONS ═══
function act(n){
  socket.emit(n);
  const m={hint:'Hint requested…',undo:'Undo requested…',save_usb:'Saving to USB…'};
  if(m[n])toast(m[n]);
}

// ═══ SYSTEM POLLING ═══
function pollSys(){
  fetch('/api/system').then(r=>r.json()).then(d=>{
    document.getElementById('sys-temp').textContent=d.cpu_temp_c!=='N/A'?d.cpu_temp_c+' °C':'—';
    document.getElementById('sys-cpu').textContent=d.cpu_pct!=='N/A'?d.cpu_pct+' %':'—';
    document.getElementById('sys-ram').textContent=d.ram_used_mb!=='N/A'?d.ram_used_mb+' / '+d.ram_total_mb+' MB':'—';
  }).catch(()=>{});
}
pollSys();setInterval(pollSys,6000);

// ═══ OTA ═══
function otaUpdate(){
  const token=prompt('OTA token:');if(!token)return;
  const restart=confirm('Restart after pull?');
  const out=document.getElementById('ota-out');out.style.display='block';out.textContent='Running git pull…\n';
  fetch('/api/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,restart})})
    .then(r=>r.json()).then(d=>{out.textContent=d.error||d.output||'Done.';if(d.restarting)out.textContent+='\nRestarting…';})
    .catch(e=>{out.textContent='Error: '+e;});
}

// ═══ TOAST ═══
function toast(msg,type='ok',ms=3000){
  const w=document.getElementById('toast-wrap'),t=document.createElement('div');
  t.className='toast '+type;t.textContent=msg;w.appendChild(t);
  requestAnimationFrame(()=>requestAnimationFrame(()=>t.classList.add('show')));
  setTimeout(()=>{t.classList.remove('show');setTimeout(()=>t.remove(),300);},ms);
}

// ═══ INIT ═══
window.addEventListener('resize',drawBoard);
new ResizeObserver(drawBoard).observe(document.getElementById('board-wrap'));
buildThemes();drawBoard();
</script>
</body>
</html>
"""