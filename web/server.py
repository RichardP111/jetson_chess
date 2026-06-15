# =============================================================================
# web/server.py
# Author : Richard Pu
# Created: 2026-06-10  |  Revised: 2026-06-15
# Purpose: Real-time web dashboard for the Jetson smart chessboard.
#          Includes real-time hardware OLED text screen mirror replication.
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
    "fen":              chess.STARTING_FEN,
    "move_history":     [],
    "whose_turn":       "White",
    "game_mode":        "—",
    "difficulty":       0,
    "last_move":        "",
    "status":           "Waiting to start",
    "thinking":         False,
    "theme":            "classic",
    "custom_theme":     None,
    "eval_score":       0,
    "hint_move":        "",
    "game_active":      False,
    "usb_available":    False,
    "voice_enabled":    False,
    "setup_phase":      "idle",    # idle | mode_select | difficulty | time | colour | ready
    "web_player":       None,      # None | "White" | "Black" — which side web controls in LocalHuman
    "disco_active":     False,
    "led_grid":         [[0,0,0]]*64,
    "draw_reason":      "",
    "oled_lines":       ["Starting setup...", "", "", ""],  # Mirrors physical row states
}
_state_lock = threading.Lock()

_callbacks = {
    "new_game":           None,
    "hint":               None,
    "set_theme":          None,
    "move_input":         None,
    "undo":               None,
    "save_usb":           None,
    "toggle_voice":       None,
    "web_mode_select":    None,   # called with (mode_str)
    "web_setup_answer":   None,   # called with (value) during setup
    "slider_preview":      None,   # called with (phase, value) on drag
    "disco":              None,   # easter egg
}


# ── Public API (called from main.py / oled modules) ───────────────────────────

def update_state(**kwargs):
    with _state_lock:
        _state.update(kwargs)
    _push_state()


def update_led_grid(grid: list):
    """Called from LEDController.chess_show() — push live LED colours."""
    with _state_lock:
        _state["led_grid"] = grid
    socketio.emit("led_grid", {"grid": grid})


def update_oled_text(lines: list):
    """Pushes a list of text rows to live mirror the hardware OLED panel on web."""
    with _state_lock:
        # Pad or clip arrays safely to preserve standard 4-row layout lines
        processed = [str(lines[i]) if i < len(lines) else "" for i in range(4)]
        _state["oled_lines"] = processed
    socketio.emit("oled_update", {"lines": processed})


def register_callbacks(**kw):
    for k, v in kw.items():
        if k in _callbacks:
            _callbacks[k] = v


def start_server(host: str | None = None, port: int | None = None):
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
            "setup_phase":   _state["setup_phase"],
            "web_player":    _state["web_player"],
            "disco_active":  _state["disco_active"],
            "led_grid":      _state["led_grid"],
            "draw_reason":   _state["draw_reason"],
            "oled_lines":    _state["oled_lines"],
        }
    socketio.emit("state_update", payload)


# ── HTTP Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    html = DASHBOARD_HTML.replace("__SPLASH_MS__", str(int(CFG.web_splash_hold_s * 1000)))
    return render_template_string(html)


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
    sid = getattr(request, "sid", "unknown"); log.info(f"Dashboard client connected: {sid}")
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
    try:
        fen    = data.get("fen") or chess.STARTING_FEN
        sq_alg = data.get("sq", "")
        board  = chess.Board(fen)
        sq     = chess.parse_square(sq_alg)
        moves  = [m.uci() for m in board.legal_moves if m.from_square == sq]
        emit("legal_moves", {"moves": moves, "sq": sq_alg})
    except Exception:
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
                         
    # ── ADD THIS CHANGE HERE ──
    # Automatically trip the OK confirmation flag to prevent the backend 
    # game loop from waiting for a physical Button 9 press.
    if "web_ok" in _callbacks and _callbacks["web_ok"]:
        _callbacks["web_ok"]()

@socketio.on("web_mode_select")
def on_web_mode_select(data):
    mode = data.get("mode", "")
    log.info(f"Web mode select: {mode}")
    if _callbacks["web_mode_select"]:
        threading.Thread(target=_callbacks["web_mode_select"],
                         args=(mode,), daemon=True).start()

@socketio.on("slider_preview")
def on_slider_preview(data):
    """Live slider drag — update OLED without confirming."""
    phase = data.get("phase", "difficulty")
    value = int(data.get("value", 0))
    cb = _callbacks.get("slider_preview")
    if cb:
        threading.Thread(target=cb, args=(phase, value), daemon=True).start()


@socketio.on("web_setup_answer")
def on_web_setup_answer(data):
    value = data.get("value")
    log.info(f"Web setup answer: {value}")
    if _callbacks["web_setup_answer"]:
        threading.Thread(target=_callbacks["web_setup_answer"],
                         args=(value,), daemon=True).start()

@socketio.on("disco")
def on_disco():
    log.info("Disco easter egg triggered from web!")
    if _callbacks["disco"]:
        threading.Thread(target=_callbacks["disco"], daemon=True).start()

@socketio.on("get_dev_settings")
def on_get_dev_settings():
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
        "dev_pin":             CFG.dev_pin,
    })

@socketio.on("apply_dev_settings")
def on_apply_dev_settings(data):
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
    _set("dev_pin",              "dev_pin",               str)

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
:root {
  --gold:       #c9a84c;
  --gold-dim:   #9a7a30;
  --gold-glow:  rgba(201,168,76,0.15);
  --bg:         #111318;
  --surface:    #1e2128;
  --surface2:   #272c36;
  --border:     #2e3340;
  --text:       #e8eaf0;
  --text-dim:   #8c93a8;
  --green:      #4caf50;
  --red:        #f44336;
  --blue:       #4fc3f7;
  --r-sm:       6px;
  --r-md:       10px;
  --r-lg:       16px;
  --r-xl:       22px;
}
[data-theme="light"] {
  --bg:       #f4f5f7;
  --surface:  #ffffff;
  --surface2: #eef0f4;
  --border:   #d0d4de;
  --text:     #1a1d26;
  --text-dim: #5a6070;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Google Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}
a{color:var(--gold);text-decoration:none}

/* ── Topbar ── */
.topbar{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100}
.topbar-left{display:flex;align-items:center;gap:12px}
.logo{font-size:18px;font-weight:600;color:var(--gold);letter-spacing:.3px}
.conn-dot{width:8px;height:8px;border-radius:50%;background:var(--red);transition:.3s}
.conn-dot.on{background:var(--green)}
.topbar-right{display:flex;align-items:center;gap:8px}
.icon-btn{background:none;border:1px solid var(--border);border-radius:var(--r-md);padding:6px 10px;cursor:pointer;color:var(--text-dim);font-size:14px;transition:.2s;display:flex;align-items:center;gap:5px}
.icon-btn:hover{border-color:var(--gold);color:var(--gold)}

/* ── Layout ── */
.main {
  display: grid;
  grid-template-columns: 1fr 340px;
  gap: 16px;
  padding: 16px;
  max-width: 1200px;
  margin: 0 auto;
}
@media(max-width:900px){
  .main { grid-template-columns: 1fr; }
  .diagnostics-row { grid-template-columns: 1fr !important; }
}

.diagnostics-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 14px;
}

/* ── Board ── */
.board-wrap{background:var(--surface);border-radius:var(--r-xl);padding:16px;border:1px solid var(--border)}
.board-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.turn-badge{padding:4px 14px;border-radius:20px;font-size:13px;font-weight:500;background:var(--gold-glow);border:1px solid var(--gold-dim);color:var(--gold)}
.board-container{position:relative;display:flex;justify-content:center}
canvas{border-radius:var(--r-md);cursor:pointer;max-width:100%;touch-action:none}
.board-footer{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}

/* ── Setup overlay on board ── */
.setup-panel{display:none;position:absolute;inset:0;background:rgba(17,19,24,.92);border-radius:var(--r-md);flex-direction:column;align-items:center;justify-content:center;gap:16px;z-index:10;padding:20px}
.setup-panel.show{display:flex}
.setup-title{font-size:18px;font-weight:600;color:var(--gold);text-align:center}
.setup-sub{font-size:13px;color:var(--text-dim);text-align:center;max-width:260px}
.setup-btns{display:flex;flex-wrap:wrap;gap:10px;justify-content:center}
.setup-btn{padding:10px 20px;border-radius:var(--r-md);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-family:'Google Sans',sans-serif;font-size:14px;font-weight:500;cursor:pointer;transition:.2s;min-width:80px}
.setup-btn:hover,.setup-btn.active{border-color:var(--gold);color:var(--gold);background:var(--gold-glow)}
.setup-slider{width:100%;max-width:260px}
.setup-slider-val{font-size:24px;font-weight:600;color:var(--gold);min-width:60px;text-align:center}

/* ── Sidebar ── */
.sidebar{display:flex;flex-direction:column;gap:12px}
.card{background:var(--surface);border-radius:var(--r-lg);padding:16px;border:1px solid var(--border)}
.card-title{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--text-dim);margin-bottom:12px}
.status-text{font-size:14px;color:var(--text);line-height:1.5}
.eval-bar-wrap{margin-top:8px;height:6px;border-radius:3px;background:var(--surface2);overflow:hidden}
.eval-bar{height:100%;border-radius:3px;background:var(--gold);transition:width .4s}

/* ── Controls ── */
.ctrl-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.ctrl-btn{padding:10px 8px;border-radius:var(--r-md);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-family:'Google Sans',sans-serif;font-size:13px;font-weight:500;cursor:pointer;transition:.2s;display:flex;align-items:center;justify-content:center;gap:5px}
.ctrl-btn:hover{border-color:var(--gold);color:var(--gold)}
.ctrl-btn.primary{background:var(--gold);border-color:var(--gold);color:#111;font-weight:600}
.ctrl-btn.primary:hover{background:var(--gold-dim)}
.ctrl-btn.danger{border-color:var(--red);color:var(--red)}
.ctrl-btn.danger:hover{background:rgba(244,67,54,.1)}
.ctrl-btn:disabled{opacity:.4;cursor:not-allowed}
.ctrl-btn.full{grid-column:1/-1; width: 100%;}

/* ── Move history ── */
.history-list{max-height:160px;overflow-y:auto;font-family:'Google Sans Mono',monospace;font-size:12px;color:var(--text-dim);display:grid;grid-template-columns:auto 1fr 1fr;gap:2px 8px}
.history-list .move-num{color:var(--text-dim)}
.history-list .move-w{color:var(--text)}
.history-list .move-b{color:var(--text-dim)}
.history-list .move-w.last,.history-list .move-b.last{color:var(--gold);font-weight:600}

/* ── Modern Theme Designer ── */
.theme-pills{display:flex;flex-wrap:wrap;gap:6px}
.theme-pill{padding:4px 12px;border-radius:20px;border:1px solid var(--border);background:var(--surface2);color:var(--text-dim);font-size:12px;cursor:pointer;transition:.2s}
.theme-pill:hover,.theme-pill.active{border-color:var(--gold);color:var(--gold)}

.theme-designer-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
  margin-top: 14px;
}
@media(max-width:400px){ .theme-designer-grid { grid-template-columns: 1fr; } }

.picker-card {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 10px 12px;
  display: flex;
  align-items: center;
  gap: 12px;
  cursor: pointer;
  transition: border-color 0.2s, transform 0.1s;
}
.picker-card:hover {
  border-color: var(--gold);
  transform: translateY(-1px);
 animate  }
.picker-card input[type="color"] {
  -webkit-appearance: none;
  border: 2px solid var(--border);
  border-radius: 50%;
  width: 38px;
  height: 38px;
  cursor: pointer;
  background: none;
  padding: 0;
}
.picker-card input[type="color"]::-webkit-color-swatch-wrapper { padding: 0; }
.picker-card input[type="color"]::-webkit-color-swatch { border: none; border-radius: 50%; }

.picker-info {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.picker-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
}
.picker-sub {
  font-size: 11px;
  color: var(--text-dim);
}
.theme-note {
  background: rgba(201, 168, 76, 0.06);
  border: 1px solid rgba(201, 168, 76, 0.15);
  border-radius: var(--r-md);
  padding: 10px 12px;
  font-size: 12px;
  color: var(--gold);
  line-height: 1.4;
  margin-top: 6px;
  text-align: left;
}

/* ── Toggle ── */
.toggle-row{display:flex;align-items:center;justify-content:space-between;padding:4px 0}
.toggle-label{font-size:13px;color:var(--text)}
label.switch{position:relative;display:inline-block;width:40px;height:22px}
label.switch input{opacity:0;width:0;height:0}
.slider-sw{position:absolute;inset:0;background:var(--border);border-radius:11px;transition:.3s;cursor:pointer}
.slider-sw:before{content:'';position:absolute;width:16px;height:16px;left:3px;bottom:3px;background:white;border-radius:50%;transition:.3s}
input:checked+.slider-sw{background:var(--gold)}
input:checked+.slider-sw:before{transform:translateX(18px)}

/* ── Overlays ── */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;z-index:200;opacity:0;pointer-events:none;transition:opacity .2s}
.overlay.show{opacity:1;pointer-events:all}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);padding:28px 24px;width:100%;max-width:420px;position:relative;max-height:90vh;overflow-y:auto}
.modal-close{position:absolute;top:14px;right:16px;background:none;border:none;color:var(--text-dim);font-size:20px;cursor:pointer;line-height:1}
.modal-close:hover{color:var(--text)}
.modal h2{font-size:18px;font-weight:600;margin-bottom:16px;color:var(--gold)}
.field{margin-bottom:12px}
.field label{display:block;font-size:12px;color:var(--text-dim);margin-bottom:4px}
.field input,.field select{width:100%;padding:8px 10px;border-radius:var(--r-sm);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-family:'Google Sans',sans-serif;font-size:13px}
.field input:focus,.field select:focus{outline:none;border-color:var(--gold)}

/* ── PIN ── */
.pin-modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:28px 24px 20px;text-align:center;width:100%;max-width:290px}
.pin-toggle{font-size:16px;font-weight:600;margin-bottom:16px}
.pin-dots{display:flex;gap:10px;justify-content:center;margin-bottom:16px}
.pd{width:14px;height:14px;border-radius:50%;border:2px solid var(--border);background:transparent;transition:.2s}
.pd.filled{background:var(--gold);border-color:var(--gold)}
.pin-numpad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.pin-key{padding:14px;border-radius:var(--r-md);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-size:18px;font-weight:600;cursor:pointer;transition:.2s}
.pin-key:hover{border-color:var(--gold);color:var(--gold)}
.pin-err{color:var(--red);font-size:12px;min-height:18px;margin-top:8px}

/* ── About ── */
.about-logo{font-size:32px;font-weight:700;color:var(--gold);text-align:center;margin-bottom:4px}
.about-ver{text-align:center;color:var(--text-dim);font-size:13px;margin-bottom:20px}
.about-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px}
.chip{padding:4px 10px;border-radius:20px;border:1px solid var(--border);font-size:11px;color:var(--text-dim)}
.heart{display:inline-block;animation:hb .8s infinite}
@keyframes hb{0%,100%{transform:scale(1)}50%{transform:scale(1.3)}}

/* ── Dev panel ── */
.dev-section{margin-bottom:14px}
.dev-section-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--text-dim);margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)}
.dev-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:8px}
.dev-row label{font-size:12px;color:var(--text-dim);flex:1}
.dev-row input{width:80px;padding:4px 8px;border-radius:var(--r-sm);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-size:12px;text-align:right}
.dev-row input:focus{outline:none;border-color:var(--gold)}
.dev-unlocked{border-color:var(--gold)!important;color:var(--gold)!important}

/* ── USB ── */
#usb-status{font-size:12px;color:var(--text-dim);margin-top:6px}

/* ── Virtual OLED Mirror Container ── */
.oled-screen-wrap{background:#000000;border:1px solid var(--border);border-radius:var(--r-md);padding:10px 12px;font-family:'Google Sans Mono',monospace;font-size:13px;color:#51dcff;text-shadow:0 0 4px rgba(81,220,255,0.55);min-height:86px;display:flex;flex-direction:column;justify-content:space-between;letter-spacing:0.3px}
.oled-row{min-height:16px;white-space:pre;overflow:hidden;text-overflow:ellipsis;transition:all 0.1s ease}

/* ── LED Grid ── */
.led-grid-wrap{display:grid;grid-template-columns:repeat(8,1fr);gap:2px;padding:4px;background:var(--surface2);border-radius:var(--r-md)}
.led-cell{aspect-ratio:1;border-radius:3px;background:#111;transition:background .1s;position:relative}
.led-cell.lit{box-shadow:0 0 6px 1px currentColor}

/* ── Disco ── */
.disco-hint{font-size:11px;color:var(--text-dim);text-align:center;margin-top:8px;cursor:pointer;opacity:.4;transition:.2s}
.disco-hint:hover{opacity:1;color:var(--gold)}
@keyframes disco-flash{0%{filter:hue-rotate(0deg) brightness(1.5)}100%{filter:hue-rotate(360deg) brightness(1.5)}}
.disco-active canvas{animation:disco-flash .3s linear infinite}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <span class="logo">♟ Chess Board</span>
    <div class="conn-dot" id="conn-dot"></div>
  </div>
  <div class="topbar-right">
    <button class="icon-btn" onclick="toggleTheme()">🌓</button>
    <button class="icon-btn" id="dev-btn" onclick="openDev()">🔒 Dev</button>
    <button class="icon-btn" onclick="document.getElementById('ov-about').classList.add('show')">v3.0</button>
  </div>
</div>

<div class="main">

  <div>
    <div class="board-wrap">
      <div class="board-header">
        <div class="turn-badge" id="turn-badge">Waiting...</div>
        <div style="font-size:12px;color:var(--text-dim)" id="mode-label">—</div>
      </div>

      <div class="board-container" id="board-container">
        <div class="setup-panel" id="setup-panel">
          <div class="setup-title" id="setup-title">Select Game Mode</div>
          <div class="setup-sub" id="setup-sub"></div>
          <div class="setup-btns" id="setup-btns"></div>
          <div id="setup-slider-wrap" style="display:none;width:100%;align-items:center;flex-direction:column;gap:8px">
            <div class="setup-slider-val" id="setup-slider-val">—</div>
            <input type="range" class="setup-slider" id="setup-slider" min="1" max="8" value="4">
            <button class="setup-btn" onclick="confirmSlider()">Confirm</button>
          </div>
        </div>
        <canvas id="board" width="480" height="480"></canvas>
      </div>

      <div class="board-footer">
        <button class="ctrl-btn" onclick="socket.emit('undo')" id="undo-btn">↩ Undo</button>
        <button class="ctrl-btn" onclick="socket.emit('hint')" id="hint-btn">💡 Hint</button>
        <button class="ctrl-btn primary full" onclick="socket.emit('new_game')" id="new-game-btn">New Game</button>
      </div>
    </div>

    <div class="diagnostics-row">
      <div class="card" style="margin-bottom:0">
        <div class="card-title">Live OLED Screen Mirror</div>
        <div class="oled-screen-wrap" id="oled-screen">
          <div class="oled-row" id="ol0"></div>
          <div class="oled-row" id="ol1"></div>
          <div class="oled-row" id="ol2"></div>
          <div class="oled-row" id="ol3"></div>
        </div>
      </div>

      <div class="card" style="margin-bottom:0">
        <div class="card-title">Live Board LEDs</div>
        <div class="led-grid-wrap" id="led-grid"></div>
      </div>
    </div>
  </div>

  <div class="sidebar">

    <div class="card">
      <div class="card-title">Status</div>
      <div class="status-text" id="status-text">Connecting...</div>
      <div class="eval-bar-wrap"><div class="eval-bar" id="eval-bar" style="width:50%"></div></div>
    </div>

    <div class="card">
      <div class="card-title">Controls</div>
      <div class="ctrl-grid">
        <button class="ctrl-btn" id="usb-btn" onclick="socket.emit('save_usb')" disabled>💾 Save USB</button>
        <a href="/api/pgn/download"><button class="ctrl-btn">📥 PGN</button></a>
        <div class="toggle-row full" style="grid-column:1/-1">
          <span class="toggle-label" id="voice-label">Announcements off</span>
          <label class="switch">
            <input type="checkbox" id="voice-toggle" onchange="socket.emit('toggle_voice')">
            <span class="slider-sw"></span>
          </label>
        </div>
      </div>
      <div id="usb-status" style="margin-top:8px;font-size:12px;color:var(--text-dim)">No USB drive detected</div>
    </div>

    <div class="card">
      <div class="card-title">Move History</div>
      <div class="history-list" id="history-list"></div>
    </div>

    <div class="card">
      <div class="card-title">Board Theme</div>
      <div class="theme-pills">
        <div class="theme-pill" data-t="classic" onclick="setTheme('classic')">Classic</div>
        <div class="theme-pill" data-t="ocean" onclick="setTheme('ocean')">Ocean</div>
        <div class="theme-pill" data-t="forest" onclick="setTheme('forest')">Forest</div>
        <div class="theme-pill" data-t="night" onclick="setTheme('night')">Night</div>
        <div class="theme-pill" data-t="custom" onclick="document.getElementById('ov-theme').classList.add('show')">Custom…</div>
      </div>
    </div>

    <div class="disco-hint" onclick="triggerDisco()" title="🕺">✨ tap here for a surprise</div>

  </div>
</div>

<div class="overlay" id="ov-theme">
  <div class="modal" style="max-width: 460px;">
    <button class="modal-close" onclick="closeOv('ov-theme')">✕</button>
    <h2>🎨 Custom LED Theme Designer</h2>
    
    <div class="theme-note">
      <strong>💡 Pro-Tip:</strong> Select your preferred base color. The smart board automatically calculates dark squares at 50% brightness to ensure a crisp, playable checkerboard pattern!
    </div>

    <div class="theme-designer-grid">
      <div class="picker-card" id="card-light" onclick="document.getElementById('c-light').click()">
        <input type="color" id="c-light" value="#f0d9b5" oninput="updateCardFeedback('light', this.value)" onclick="event.stopPropagation()">
        <div class="picker-info">
          <span class="picker-label">Base Square</span>
          <span class="picker-sub">Tap to choose</span>
        </div>
      </div>

      <div class="picker-card" id="card-sel" onclick="document.getElementById('c-sel').click()">
        <input type="color" id="c-sel" value="#aaeeff" oninput="updateCardFeedback('sel', this.value)" onclick="event.stopPropagation()">
        <div class="picker-info">
          <span class="picker-label">Selected Piece</span>
          <span class="picker-sub">Active lift cells</span>
        </div>
      </div>

      <div class="picker-card" id="card-move" onclick="document.getElementById('c-move').click()">
        <input type="color" id="c-move" value="#e8e800" oninput="updateCardFeedback('move', this.value)" onclick="event.stopPropagation()">
        <div class="picker-info">
          <span class="picker-label">Last Move</span>
          <span class="picker-sub">Trace pathway</span>
        </div>
      </div>

      <div class="picker-card" id="card-hint" onclick="document.getElementById('c-hint').click()">
        <input type="color" id="c-hint" value="#5588dd" oninput="updateCardFeedback('hint', this.value)" onclick="event.stopPropagation()">
        <div class="picker-info">
          <span class="picker-label">Tactical Hint</span>
          <span class="picker-sub">Engine advice</span>
        </div>
      </div>

      <div class="picker-card" id="card-legal" onclick="document.getElementById('c-legal').click()">
        <input type="color" id="c-legal" value="#40a020" oninput="updateCardFeedback('legal', this.value)" onclick="event.stopPropagation()">
        <div class="picker-info">
          <span class="picker-label">Legal Dest</span>
          <span class="picker-sub">Valid target squares</span>
        </div>
      </div>
    </div>

    <button class="ctrl-btn primary full" style="margin-top:20px" onclick="applyCustomTheme()">Apply Theme Settings</button>
  </div>
</div>

<div class="overlay" id="ov-pin">
  <div class="pin-modal">
    <div class="pin-title">🔒 Dev Access</div>
    <div class="pin-dots">
      <div class="pd" id="pd0"></div><div class="pd" id="pd1"></div>
      <div class="pd" id="pd2"></div><div class="pd" id="pd3"></div>
    </div>
    <div class="pin-numpad">
      <button class="pin-key" onclick="pk('1')">1</button>
      <button class="pin-key" onclick="pk('2')">2</button>
      <button class="pin-key" onclick="pk('3')">3</button>
      <button class="pin-key" onclick="pk('4')">4</button>
      <button class="pin-key" onclick="pk('5')">5</button>
      <button class="pin-key" onclick="pk('6')">6</button>
      <button class="pin-key" onclick="pk('7')">7</button>
      <button class="pin-key" onclick="pk('8')">8</button>
      <button class="pin-key" onclick="pk('9')">9</button>
      <button class="pin-key" onclick="closeOv('ov-pin')" style="font-size:14px">✕</button>
      <button class="pin-key" onclick="pk('0')">0</button>
      <button class="pin-key" onclick="pdel()">⌫</button>
    </div>
    <div class="pin-err" id="pin-err"></div>
  </div>
</div>

<div class="overlay" id="ov-dev">
  <div class="modal" style="max-width:500px">
    <button class="modal-close" onclick="closeOv('ov-dev')">✕</button>
    <h2>⚙️ Developer Settings</h2>

    <div class="dev-section">
      <div class="dev-section-title">AI Engine</div>
      <div class="dev-row"><label>Default difficulty</label><input type="number" id="d-difficulty_default" min="1" max="20"></div>
      <div class="dev-row"><label>Min difficulty</label><input type="number" id="d-difficulty_min" min="1" max="20"></div>
      <div class="dev-row"><label>Max difficulty</label><input type="number" id="d-difficulty_max" min="1" max="20"></div>
      <div class="dev-row"><label>Move time default (ms)</label><input type="number" id="d-movetime_default_ms" min="500"></div>
      <div class="dev-row"><label>Move time min (ms)</label><input type="number" id="d-movetime_min_ms" min="500"></div>
      <div class="dev-row"><label>Move time max (ms)</label><input type="number" id="d-movetime_max_ms" min="500"></div>
      <div class="dev-row"><label>Stockfish path</label><input type="text" id="d-stockfish_path" style="width:160px"></div>
    </div>

    <div class="dev-section">
      <div class="dev-section-title">Hardware</div>
      <div class="dev-row"><label>LED brightness (0-255) <span style="color:var(--red);font-size:10px">⚠ power limit!</span></label><input type="number" id="d-chess_led_brightness" min="0" max="255" data-caution="true"></div>
      <div class="dev-row"><label>Startup LED delay (s)</label><input type="number" id="d-startup_led_delay_s" step="0.001"></div>
      <div class="dev-row"><label>Button debounce (s)</label><input type="number" id="d-button_debounce_s" step="0.01"></div>
    </div>

    <div class="dev-section">
      <div class="dev-section-title">Gameplay</div>
      <div class="dev-row"><label>Hint dismiss (s)</label><input type="number" id="d-hint_dismiss_s" step="0.5"></div>
      <div class="dev-row"><label>Max undo half-moves</label><input type="number" id="d-undo_max_half_moves" min="1"></div>
    </div>

    <div class="dev-section">
      <div class="dev-section-title">Voice</div>
      <div class="dev-row"><label>TTS rate (wpm)</label><input type="number" id="d-tts_rate" min="80" max="300"></div>
      <div class="dev-row"><label>TTS volume (0-1)</label><input type="number" id="d-tts_volume" step="0.1" min="0" max="1"></div>
    </div>

    <div class="dev-section">
      <div class="dev-section-title">Server</div>
      <div class="dev-row"><label>Web port</label><input type="number" id="d-web_port"></div>
      <div class="dev-row"><label>PGN subdirectory</label><input type="text" id="d-pgn_subdir" style="width:120px"></div>
      <div class="dev-row"><label>Dev PIN</label><input type="text" id="d-dev_pin" maxlength="8" style="width:80px"></div>
    </div>

    <button class="ctrl-btn primary full" style="margin-top:4px" onclick="saveDevSettings()">Save Settings</button>
    <button class="ctrl-btn full" style="margin-top:6px" onclick="closeOv('ov-dev')">Close</button>
    <div id="dev-save-msg" style="font-size:12px;color:var(--green);margin-top:8px;min-height:16px"></div>
  </div>
</div>

<div class="overlay" id="ov-about" onclick="closeOv('ov-about')">
  <div class="modal" style="text-align:center" onclick="event.stopPropagation()">
    <button class="modal-close" onclick="closeOv('ov-about')">✕</button>
    <div class="about-logo">♟</div>
    <div style="font-size:20px;font-weight:700;color:var(--gold);margin-bottom:4px">Smart Chess Board</div>
    <div class="about-ver">v3.0 · Jetson Orin Nano</div>
    <div class="about-chips">
      <span class="chip">Python 3</span>
      <span class="chip">Flask + SocketIO</span>
      <span class="chip">python-chess</span>
      <span class="chip">Stockfish</span>
      <span class="chip">Jetson.GPIO</span>
      <span class="chip">WS2812b LEDs</span>
      <span class="chip">SSD1306 OLED</span>
    </div>
    <div style="font-size:14px;color:var(--text-dim)">Made with <span class="heart">❤️</span> by Richard P</div>
    <div style="font-size:12px;color:var(--text-dim);margin-top:12px;opacity:.5;cursor:pointer" onclick="triggerDisco()">psst...</div>
    <div style="margin-top:16px"><a href="https://github.com/RichardP111/jetson_chess" target="_blank" style="font-size:12px;color:var(--gold);opacity:.8">&#128279; github.com/RichardP111/jetson_chess</a></div>
  </div>
</div>

<script>
const socket = io();
let currentFen  = null;
let selectedSq  = null;
let legalMoveSqs = [];
let lastMove    = null;
let hintMove    = null;
let currentTheme= 'classic';
let gameActive  = false;
let setupPhase  = 'idle';
let webPlayer   = null;
let hintCount   = 0;
let discoActive = false;
let currentSliderPhase = 'difficulty';

const THEMES = {
  classic:{ light:'#f0d9b5', dark:'#b58863', sel:'#aaeeff', move:'#e8e800', hint:'#5588dd', legal:'#40a020' },
  ocean:  { light:'#c9e8f0', dark:'#3a7fb5', sel:'#aaffdd', move:'#ffe040', hint:'#ff8040', legal:'#20c060' },
  forest: { light:'#e8f0c0', dark:'#5a8040', sel:'#ffd0a0', move:'#ffff60', hint:'#60a0ff', legal:'#30d060' },
  night:  { light:'#3a3f50', dark:'#1e2230', sel:'#8080ff', move:'#ffcc00', hint:'#ff6060', legal:'#40cc40' },
};
let customTheme = null;
let activeTheme = THEMES.classic;

socket.on('connect', () => {
  document.getElementById('conn-dot').classList.add('on');
});
socket.on('disconnect', ()=>{ document.getElementById('conn-dot').classList.remove('on'); });

function updateCardFeedback(type, hexColor) {
  const card = document.getElementById('card-' + type);
  if (card) {
    card.style.borderColor = hexColor;
    card.style.boxShadow = `0 0 10px ${hexColor}40`;
  }
}

function applyCustomTheme() {
  const lightHex = document.getElementById('c-light').value;
  const r = Math.floor(parseInt(lightHex.slice(1, 3), 16) / 2).toString(16).padStart(2, '0');
  const g = Math.floor(parseInt(lightHex.slice(3, 5), 16) / 2).toString(16).padStart(2, '0');
  const b = Math.floor(parseInt(lightHex.slice(5, 7), 16) / 2).toString(16).padStart(2, '0');
  const darkHex = `#${r}${g}${b}`;

  const ct = {
    light: lightHex,
    dark:  darkHex,
    sel:   document.getElementById('c-sel').value,
    move:  document.getElementById('c-move').value,
    hint:  document.getElementById('c-hint').value,
    legal: document.getElementById('c-legal').value,
  };
  
  customTheme = ct;
  socket.emit('set_custom_theme', ct);
  closeOv('ov-theme');
}

socket.on('state_update', d => {
  currentFen  = d.fen;
  lastMove    = d.last_move  || null;
  hintMove    = d.hint_move  || null;
  gameActive  = d.game_active;
  setupPhase  = d.setup_phase || 'idle';
  webPlayer   = d.web_player || null;
  discoActive = d.disco_active || false;

  if (d.setup_phase && d.setup_phase !== 'idle' && d.setup_phase !== 'ready') {
    document.getElementById('turn-badge').textContent = 'Setting up...';
  } else if (d.thinking) {
    document.getElementById('turn-badge').textContent = 'Thinking...';
  } else if (!d.game_active) {
    document.getElementById('turn-badge').textContent = 'Waiting...';
  } else {
    document.getElementById('turn-badge').textContent = d.whose_turn + "'s turn";
  }
  document.getElementById('mode-label').textContent = d.game_mode || '—';
  document.getElementById('status-text').textContent = d.status || '';

  const evalPct = Math.max(5, Math.min(95, 50 + (d.eval_score || 0) / 20));
  document.getElementById('eval-bar').style.width = evalPct + '%';

  document.getElementById('usb-btn').disabled = !d.usb_available;
  document.getElementById('usb-status').textContent =
    d.usb_available ? 'USB ready — click to save' : 'No USB drive detected';

  document.getElementById('voice-toggle').checked = !!d.voice_enabled;
  document.getElementById('voice-label').textContent =
    d.voice_enabled ? 'Announcements on' : 'Announcements off';

  const canUseGameplayCtrls = d.game_active && (d.setup_phase === 'idle' || d.setup_phase === 'ready');
  document.getElementById('undo-btn').style.display = canUseGameplayCtrls ? 'flex' : 'none';
  document.getElementById('hint-btn').style.display = canUseGameplayCtrls ? 'flex' : 'none';

  syncThemes(d.theme);
  renderHistory(d.move_history || []);
  drawBoard();
  handleSetupPhase(d);

  if(d.oled_lines) updateOledMirror(d.oled_lines);

  if (d.theme === 'custom' && d.custom_theme) {
    customTheme = d.custom_theme;
    syncThemes('custom');
  }
  
  ['light', 'sel', 'move', 'hint', 'legal'].forEach(key => {
    const input = document.getElementById('c-' + key);
    if (input && activeTheme[key]) {
      input.value = activeTheme[key];
      updateCardFeedback(key, activeTheme[key]);
    }
  });

  document.getElementById('board-container').classList.toggle('disco-active', discoActive);
});

socket.on('legal_moves', d => {
  legalMoveSqs = d.moves.map(m => m.slice(2,4));
  drawBoard();
});

socket.on('move_rejected', d => {
  document.getElementById('status-text').textContent = '❌ ' + d.reason;
});

socket.on('dev_settings', d => {
  Object.entries(d).forEach(([k,v]) => {
    const el = document.getElementById('d-'+k);
    if (el) el.value = v;
  });
  const el = document.getElementById('d-chess_led_brightness');
  if (el && d.chess_led_brightness !== undefined)
    el.dataset.origVal = String(d.chess_led_brightness);
});

socket.on('dev_settings_saved', d => {
  const msg = document.getElementById('dev-save-msg');
  if (d.ok) {
    msg.style.color = 'var(--green)';
    msg.textContent = 'Saved (' + d.changed.length + ' changes)';
    setTimeout(() => { closeOv('ov-dev'); msg.textContent = ''; }, 1500);
  } else {
    msg.style.color = 'var(--red)';
    msg.textContent = 'Save error';
    setTimeout(() => msg.textContent = '', 3000);
  }
});

socket.on('oled_update', d => { if(d.lines) updateOledMirror(d.lines); });

function handleSetupPhase(d) {
  const panel = document.getElementById('setup-panel');
  const phase = d.setup_phase || 'idle';

  if (phase === 'idle' || phase === 'ready') {
    panel.classList.remove('show');
    return;
  }

  panel.classList.add('show');
  const title  = document.getElementById('setup-title');
  const sub    = document.getElementById('setup-sub');
  const btns   = document.getElementById('setup-btns');
  const slWrap = document.getElementById('setup-slider-wrap');
  const slider = document.getElementById('setup-slider');
  const slVal  = document.getElementById('setup-slider-val');
  btns.innerHTML = '';
  slWrap.style.display = 'none';

  if (phase === 'mode_select') {
    title.textContent = 'Select Game Mode';
    sub.textContent = 'Choose how you want to play';
    [
      ['stockfish',   '🤖 vs AI'],
      ['lichess',     '🌐 Lichess Online'],
      ['local',       '👥 Local 2 Player'],
      ['web_vs_ai',   '💻 Web vs AI'],
      ['web_local',   '💻 Web 2 Player'],
    ].forEach(([mode, label]) => {
      const b = document.createElement('button');
      b.className = 'setup-btn';
      b.textContent = label;
      b.onclick = () => socket.emit('web_mode_select', {mode});
      btns.appendChild(b);
    });
  } else if (phase === 'difficulty') {
    title.textContent = 'Set AI Difficulty';
    sub.textContent = 'Drag the slider (1 = easiest, 8 = hardest)';
    currentSliderPhase = 'difficulty';
    slWrap.style.display = 'flex';
    slider.min = 1; slider.max = 8; slider.value = 1;
    slVal.textContent = '1';
    socket.emit('slider_preview', {phase: 'difficulty', value: 1});
    slider.oninput = () => { slVal.textContent = slider.value; socket.emit('slider_preview', {phase: currentSliderPhase, value: parseInt(slider.value)}); };
  } else if (phase === 'time') {
    title.textContent = 'Set Move Time';
    sub.textContent = 'How long should the engine think per move?';
    [[1,'1s'],[2,'2s'],[3,'3s'],[4,'5s'],[5,'8s'],[6,'12s'],[7,'20s'],[8,'30s']].forEach(([idx, label]) => {
      const b = document.createElement('button');
      b.className = 'setup-btn';
      b.textContent = label;
      b.onclick = () => { socket.emit('slider_preview', {phase:'time', value:idx}); setTimeout(()=>socket.emit('web_setup_answer', {value: idx}), 50); };
      btns.appendChild(b);
    });
  } else if (phase === 'colour') {
    title.textContent = 'Choose Your Colour';
    sub.textContent = '';
    [['white','♔ White'],['black','♚ Black']].forEach(([c, label]) => {
      const b = document.createElement('button');
      b.className = 'setup-btn';
      b.textContent = label;
      b.onclick = () => socket.emit('web_setup_answer', {value: c});
      btns.appendChild(b);
    });
  } else if (phase === 'web_colour') {
    title.textContent = 'Web Player Colour';
    sub.textContent = 'Which side do you control from the browser?';
    [['White','♔ White'],['Black','♚ Black']].forEach(([c, label]) => {
      const b = document.createElement('button');
      b.className = 'setup-btn';
      b.textContent = label;
      b.onclick = () => socket.emit('web_setup_answer', {value: c});
      btns.appendChild(b);
    });
  }
}

function confirmSlider() {
  const v = parseInt(document.getElementById('setup-slider').value);
  socket.emit('web_setup_answer', {value: v});
}

const canvas = document.getElementById('board');
const ctx    = canvas.getContext('2d');
const SZ     = 60;

const PIECES = {
  wK:'♔',wQ:'♕',wR:'♖',wB:'♗',wN:'♘',wP:'♙',
  bK:'♚',bQ:'♛',bR:'♜',bB:'♝',bN:'♞',bP:'♟',
};

function sqToXY(sq) {
  const f = sq.charCodeAt(0) - 97;
  const r = parseInt(sq[1]) - 1;
  return {x: f*SZ, y: (7-r)*SZ};
}

function xyToSq(x, y) {
  const f = Math.floor(x/SZ);
  const r = 7 - Math.floor(y/SZ);
  if (f<0||f>7||r<0||r>7) return null;
  return String.fromCharCode(97+f) + (r+1);
}

function parseFen(fen) {
  const pieces = {};
  const rows = fen.split(' ')[0].split('/');
  for (let r=0; r<8; r++) {
    let f = 0;
    for (const ch of rows[r]) {
      if ('12345678'.includes(ch)) { f += parseInt(ch); }
      else {
        const colour = ch === ch.toUpperCase() ? 'w' : 'b';
        const type   = ch.toUpperCase();
        pieces[String.fromCharCode(97+f) + (8-r)] = colour + type;
        f++;
      }
    }
  }
  return pieces;
}

function drawBoard() {
  if (!currentFen) return;
  const pieces = parseFen(currentFen);
  const t = activeTheme;

  ctx.clearRect(0,0,480,480);

  for (let r=0; r<8; r++) {
    for (let f=0; f<8; f++) {
      const sq  = String.fromCharCode(97+f) + (8-r);
      const isLight = (f+r)%2===0;
      let col = isLight ? t.light : t.dark;

      if (selectedSq === sq) col = t.sel;
      else if (lastMove && (lastMove.slice(0,2)===sq || lastMove.slice(2,4)===sq)) col = t.move;
      else if (hintMove && (hintMove.slice(0,2)===sq || hintMove.slice(2,4)===sq)) col = t.hint;
      else if (legalMoveSqs.includes(sq)) col = t.legal;

      ctx.fillStyle = col;
      ctx.fillRect(f*SZ, r*SZ, SZ, SZ);

      if (pieces[sq]) {
        ctx.font = `${SZ*0.7}px serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        const isDark = pieces[sq][0]==='b';
        ctx.fillStyle = isDark ? '#111' : '#fff';
        ctx.strokeStyle = isDark ? '#fff' : '#111';
        ctx.lineWidth = 2;
        ctx.strokeText(PIECES[pieces[sq]], f*SZ+SZ/2, r*SZ+SZ/2);
        ctx.fillText(PIECES[pieces[sq]],   f*SZ+SZ/2, r*SZ+SZ/2);
      }

      if (f===0) {
        ctx.fillStyle = isLight ? t.dark : t.light;
        ctx.font = '10px Google Sans,sans-serif';
        ctx.textAlign = 'left'; ctx.textBaseline = 'top';
        ctx.fillText(8-r, 2, r*SZ+2);
      }
      if (r===7) {
        ctx.fillStyle = isLight ? t.dark : t.light;
        ctx.font = '10px Google Sans,sans-serif';
        ctx.textAlign = 'right'; ctx.textBaseline = 'bottom';
        ctx.fillText(String.fromCharCode(97+f), (f+1)*SZ-2, 8*SZ-2);
      }
    }
  }
}

canvas.addEventListener('click', e => {
  if (!gameActive || setupPhase !== 'idle') return;
  const rect = canvas.getBoundingClientRect();
  const scaleX = 480 / rect.width;
  const scaleY = 480 / rect.height;
  const x = (e.clientX - rect.left) * scaleX;
  const y = (e.clientY - rect.top)  * scaleY;
  const sq = xyToSq(x, y);
  if (!sq) return;

  if (selectedSq) {
    const uci = selectedSq + sq;
    selectedSq = null;
    legalMoveSqs = [];
    socket.emit('move_input', {uci});
    drawBoard();
  } else {
    selectedSq = sq;
    legalMoveSqs = [];
    socket.emit('get_legal_moves', {fen: currentFen, sq});
    drawBoard();
  }
});

function renderHistory(history) {
  const el = document.getElementById('history-list');
  el.innerHTML = '';
  for (let i=0; i<history.length; i+=2) {
    const num = document.createElement('span');
    num.className = 'move-num'; num.textContent = (i/2+1)+'.';
    const w = document.createElement('span');
    w.className = 'move-w' + (i===history.length-1 ? ' last':'');
    w.textContent = history[i];
    const b = document.createElement('span');
    b.className = 'move-b' + (i+1===history.length-1 ? ' last':'');
    b.textContent = history[i+1] || '';
    el.appendChild(num); el.appendChild(w); el.appendChild(b);
  }
  el.scrollTop = el.scrollHeight;
}

function setTheme(t) {
  socket.emit('set_theme', {theme: t});
}
function syncThemes(t) {
  currentTheme = t;
  activeTheme  = t === 'custom' && customTheme ? customTheme : (THEMES[t] || THEMES.classic);
  document.querySelectorAll('.theme-pill').forEach(p =>
    p.classList.toggle('active', p.dataset.t === t));
  drawBoard();
}

function updateOledMirror(lines) {
  for(let i=0; i<4; i++) {
    const el = document.getElementById('ol'+i);
    if (!el) continue;
    const txt = lines[i] !== undefined ? lines[i] : '';
    el.textContent = txt;
    
    if (i === 0 && txt && !txt.startsWith(' ')) {
      el.style.background = '#51dcff';
      el.style.color = '#000000';
      el.style.textShadow = 'none';
      el.style.padding = '1px 4px';
      el.style.borderRadius = '2px';
      el.style.fontWeight = '600';
    } else {
      el.style.background = 'none';
      el.style.color = '#51dcff';
      el.style.textShadow = '0 0 4px rgba(81,220,255,0.55)';
      el.style.padding = '0';
      el.style.borderRadius = '0';
      el.style.fontWeight = 'normal';
    }
  }
}

let pinBuf='', devUnlocked=false;

function openDev() {
  if (devUnlocked) { showDevPanel(); return; }
  pinBuf=''; updPinDots();
  document.getElementById('pin-err').textContent='';
  document.getElementById('ov-pin').classList.add('show');
}
function pk(d) {
  if (pinBuf.length>=4) return;
  pinBuf+=d; updPinDots();
  if (pinBuf.length===4) checkPin();
}
function pdel() { pinBuf=pinBuf.slice(0,-1); updPinDots(); document.getElementById('pin-err').textContent=''; }
function updPinDots() {
  for(let i=0;i<4;i++){
    document.getElementById('pd'+i).className='pd'+(i<pinBuf.length?' filled':'');
  }
}
function checkPin() {
  socket.emit('get_dev_settings');
  socket._pendingPinCheck = pinBuf;
}

socket.on('dev_settings', d => {
  if (socket._pendingPinCheck !== undefined) {
    const serverPin = String(d.dev_pin || '1337');
    if (socket._pendingPinCheck === serverPin) {
      devUnlocked=true; closeOv('ov-pin'); showDevPanel();
      document.getElementById('dev-btn').classList.add('dev-unlocked');
    } else {
      document.getElementById('pin-err').textContent='Incorrect PIN';
      pinBuf=''; updPinDots();
    }
    socket._pendingPinCheck = undefined;
  }
  Object.entries(d).forEach(([k,v]) => {
    const el = document.getElementById('d-'+k);
    if (el) el.value = v;
  });
});

function showDevPanel() { socket.emit('get_dev_settings'); document.getElementById('ov-dev').classList.add('show'); }

function saveDevSettings() {
  const fields = ['difficulty_default','difficulty_min','difficulty_max',
    'movetime_default_ms','movetime_min_ms','movetime_max_ms',
    'chess_led_brightness','startup_led_delay_s','button_debounce_s',
    'hint_dismiss_s','undo_max_half_moves','stockfish_path',
    'tts_rate','tts_volume','web_port','pgn_subdir','dev_pin'];
  const brightnessEl = document.getElementById('d-chess_led_brightness');
  if (brightnessEl && brightnessEl.dataset.origVal !== undefined &&
      brightnessEl.value !== brightnessEl.dataset.origVal) {
    const newVal = parseInt(brightnessEl.value);
    if (!confirm(
      'WARNING: Changing LED brightness affects power draw.\n\n' +
      'Values above 100 may draw excessive current and damage hardware.\n' +
      'Recommended max: 76 (approx 30%).\n\nSet brightness to ' + newVal + '?'
    )) {
      brightnessEl.value = brightnessEl.dataset.origVal;
      return;
    }
  }
  const payload = {};
  fields.forEach(f => {
    const el = document.getElementById('d-'+f);
    if (el) payload[f] = el.value;
  });
  socket.emit('apply_dev_settings', payload);
}

function triggerDisco() {
  socket.emit('disco');
}

socket.on('state_update', d => {
  if (d.hint_move && d.hint_move !== hintMove) {
    hintCount++;
    if (hintCount >= 10) { hintCount=0; triggerDisco(); }
  }
});

function closeOv(id) { document.getElementById(id).classList.remove('show'); }
function toggleTheme() {
  const html = document.documentElement;
  html.dataset.theme = html.dataset.theme==='dark' ? 'light' : 'dark';
}

document.addEventListener('keydown', e => {
  if (e.key==='Escape') {
    ['ov-pin','ov-dev','ov-about','ov-theme'].forEach(closeOv);
  }
});

['ov-about','ov-theme'].forEach(id => {
  document.getElementById(id).addEventListener('click', function(e) {
    if (e.target===this) closeOv(id);
  });
});

drawBoard();

(function() {
  const splash = document.createElement('div');
  splash.id = 'splash';
  splash.innerHTML = `
    <div style="font-size:52px;margin-bottom:20px">&#9823;</div>
    <div style="font-size:22px;font-weight:700;color:var(--gold);letter-spacing:.5px;margin-bottom:6px">Smart Chess Board</div>
    <div style="font-size:13px;color:var(--text-dim);margin-bottom:28px">Connecting to board...</div>
    <div style="width:180px;height:3px;background:var(--border);border-radius:2px;overflow:hidden">
      <div id="splash-bar" style="height:100%;width:0;background:var(--gold);border-radius:2px;transition:width .3s ease"></div>
    </div>
    <div style="position:absolute;bottom:22px;font-size:11px;color:var(--text-dim);opacity:.45;text-align:center">
      Made with &#10084; by Richard P
    </div>`;
  Object.assign(splash.style, {
    position:'fixed', inset:'0', background:'var(--bg)',
    display:'flex', flexDirection:'column', alignItems:'center',
    justifyContent:'center', zIndex:'9999', transition:'opacity .5s ease'
  });
  document.body.appendChild(splash);

  const bar = () => document.getElementById('splash-bar');
  const HOLD = __SPLASH_MS__;   
  const START = Date.now();

  const iv = setInterval(() => {
    const elapsed = Date.now() - START;
    const pct = Math.min(100, (elapsed / HOLD) * 100);
    const b = bar(); if (b) b.style.width = pct + '%';
    if (pct >= 100) clearInterval(iv);
  }, 30);  

  function hideSplash() {
    clearInterval(iv);
    const b = bar(); if (b) b.style.width = '100%';
    setTimeout(() => {
      splash.style.opacity = '0';
      setTimeout(() => splash.remove(), 500);
    }, 100);
  }

  setTimeout(hideSplash, HOLD);
})();

// ═══ LED GRID GENERATOR AND MANAGER ═══
(function(){
  const grid = document.getElementById('led-grid');
  for(let i=0;i<64;i++){
    const c=document.createElement('div');
    c.className='led-cell';
    c.id='lc'+i;
    grid.appendChild(c);
  }

  function updateGrid(pixels){
    for(let i=0;i<64;i++){
      const [r,g,b] = pixels[i] || [0,0,0];
      const el = document.getElementById('lc'+i);
      if (!el) continue;
      const bright = r+g+b;
      if(bright < 10){
        el.style.background = '#111';
        el.style.boxShadow = 'none';
      } else {
        const hex = '#'+[r,g,b].map(x=>x.toString(16).padStart(2,'0')).join('');
        el.style.background = hex;
        el.style.boxShadow = `0 0 6px 2px ${hex}66`;
      }
    }
  }

  socket.on('led_grid', d => updateGrid(d.grid));
  socket.on('state_update', d => { if(d.led_grid) updateGrid(d.led_grid); });
})();
</script>
<div style="text-align:center;padding:20px 0 28px;font-size:11px;color:var(--text-dim);opacity:.4">
  Made with <span style="color:#e05050;animation:hb .8s infinite;display:inline-block">&#10084;</span> by Richard P &nbsp;&middot;&nbsp;
  <a href="https://github.com/RichardP111/jetson_chess" target="_blank" style="color:var(--text-dim)">GitHub</a>
  &nbsp;&middot;&nbsp; v3.0
</div>
</body>
</html>
"""