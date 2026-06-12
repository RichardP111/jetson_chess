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
    "fen":          chess.STARTING_FEN,
    "move_history": [],
    "whose_turn":   "White",
    "game_mode":    "—",
    "difficulty":   0,
    "last_move":    None,
    "status":       "Waiting to start",
    "thinking":     False,
    "theme":        "classic",
    "custom_theme": None,          # dict or None
    "eval_score":   0,
    "hint_move":    "",
    "game_active":  False,
    "usb_available": False,
    "voice_enabled": False,
}
_state_lock = threading.Lock()

_callbacks = {
    "new_game":   None,
    "hint":       None,
    "set_theme":  None,
    "move_input": None,   # callable(uci_str)
    "undo":       None,
    "save_usb":   None,
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
        board  = chess.Board(_state["fen"])
        last   = _state["last_move"]
        arrows = []
        if last:
            try:
                move   = chess.Move.from_uci(last)
                arrows = [chess.svg.Arrow(move.from_square, move.to_square,
                                          color="#00ff00")]
            except Exception:
                pass

        hint = _state.get("hint_move", "")
        if hint and len(hint) >= 4:
            try:
                hm = chess.Move.from_uci(hint)
                arrows.append(chess.svg.Arrow(hm.from_square, hm.to_square,
                                              color="#00ccff"))
            except Exception:
                pass

        svg = chess.svg.board(
            board, arrows=arrows, size=360,
            colors={
                "square light":          "#f0d9b5",
                "square dark":           "#b58863",
                "square light lastmove": "#cdd16a",
                "square dark lastmove":  "#aaa23a",
            },
        )
        payload = {
            "board_svg":     svg,
            "move_history":  _state["move_history"][-40:],
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
    """Stream the last game as a PGN file download."""
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
    """
    OTA update: git pull + optional service restart.
    Protected by OTA_TOKEN env var — must match the 'token' field in the POST body.
    """
    ota_token = os.environ.get("OTA_TOKEN", "")
    if not ota_token:
        return jsonify({"error": "OTA_TOKEN not set in .env — update disabled"}), 403

    data = request.get_json(silent=True) or {}
    if data.get("token") != ota_token:
        return jsonify({"error": "Invalid OTA token"}), 403

    try:
        result = subprocess.run(
            ["git", "pull"],
            capture_output=True, text=True, timeout=60,
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
    log.info("New game requested via dashboard")
    if _callbacks["new_game"]:
        threading.Thread(target=_callbacks["new_game"], daemon=True).start()


@socketio.on("hint")
def on_hint():
    log.info("Hint requested via dashboard")
    if _callbacks["hint"]:
        threading.Thread(target=_callbacks["hint"], daemon=True).start()


@socketio.on("undo")
def on_undo():
    log.info("Undo requested via dashboard")
    if _callbacks["undo"]:
        threading.Thread(target=_callbacks["undo"], daemon=True).start()


@socketio.on("save_usb")
def on_save_usb():
    log.info("USB save requested via dashboard")
    if _callbacks["save_usb"]:
        threading.Thread(target=_callbacks["save_usb"], daemon=True).start()


@socketio.on("toggle_voice")
def on_toggle_voice():
    log.info("Voice toggle requested via dashboard")
    if _callbacks["toggle_voice"]:
        threading.Thread(target=_callbacks["toggle_voice"], daemon=True).start()


@socketio.on("set_theme")
def on_set_theme(data):
    theme = data.get("theme", "classic")
    log.info(f"Theme change via dashboard: {theme}")
    update_state(theme=theme)
    if _callbacks["set_theme"]:
        _callbacks["set_theme"](theme)


@socketio.on("set_custom_theme")
def on_set_custom_theme(data):
    """Receive custom theme dict {light, dark, move, hint} as hex strings."""
    log.info(f"Custom theme via dashboard: {data}")
    with _state_lock:
        _state["theme"]        = "custom"
        _state["custom_theme"] = data
    if _callbacks["set_theme"]:
        _callbacks["set_theme"](data)
    _push_state()


@socketio.on("move_input")
def on_move_input(data):
    """Accept a UCI move from the browser (e.g. {'uci': 'e2e4'})."""
    uci = (data.get("uci") or "").strip().lower()
    if len(uci) < 4:
        emit("move_rejected", {"reason": "Too short"})
        return
    log.info(f"Move input from dashboard: {uci}")
    if _callbacks["move_input"]:
        threading.Thread(target=_callbacks["move_input"],
                         args=(uci,), daemon=True).start()


# ── Dashboard HTML ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Smart Chess Board</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
  :root {
    --bg: #1a1a2e; --surface: #16213e; --border: #0f3460;
    --accent: #e94560; --text: #eaeaea; --muted: #888;
    --green: #00ff88; --blue: #00ccff; --orange: #ffaa00;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; min-height: 100vh; }

  header { background: var(--surface); border-bottom: 2px solid var(--border);
           padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 1.3rem; color: var(--green); }
  .badge { background: var(--border); padding: 3px 10px; border-radius: 12px;
           font-size: 0.75rem; color: var(--blue); }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green);
         animation: pulse 1.5s infinite; margin-left: auto; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

  .layout { display: grid; grid-template-columns: 1fr 370px; gap: 16px;
            padding: 16px; max-width: 1100px; margin: 0 auto; }

  .panel { background: var(--surface); border: 1px solid var(--border);
           border-radius: 10px; padding: 14px; }
  .panel h3 { color: var(--blue); font-size: 0.85rem; text-transform: uppercase;
              letter-spacing: 1px; margin-bottom: 10px; }

  #board-panel { display: flex; flex-direction: column; align-items: center; gap: 10px; }
  #board-svg svg { width: 100%; max-width: 360px; border-radius: 6px; }

  .turn-banner { width: 100%; padding: 8px; text-align: center; border-radius: 6px;
                 font-weight: bold; font-size: 0.95rem; transition: all 0.3s; }
  .turn-white { background: #f0d9b5; color: #1a1a1a; }
  .turn-black { background: #1a1a1a; color: #f0d9b5; border: 1px solid #444; }
  .thinking   { background: #0f3460 !important; color: var(--blue) !important;
                animation: blink 0.8s infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.5} }

  .eval-bar  { width: 100%; height: 14px; background: #333; border-radius: 7px; overflow: hidden; }
  .eval-fill { height: 100%; background: linear-gradient(90deg,#fff 0%,#fff var(--pct),#222 var(--pct),#222 100%);
               transition: --pct 0.5s; }
  .eval-label { text-align: center; font-size: 0.72rem; color: var(--muted); margin-top: 3px; }

  .right-col { display: flex; flex-direction: column; gap: 12px; }

  .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .stat { background: var(--border); border-radius: 6px; padding: 8px 10px; }
  .stat label { font-size: 0.7rem; color: var(--muted); display: block; }
  .stat value { font-size: 1.1rem; font-weight: bold; color: var(--green); }

  .move-list { max-height: 200px; overflow-y: auto; font-family: monospace; font-size: 0.82rem; line-height: 1.6; }
  .move-list::-webkit-scrollbar { width: 4px; }
  .move-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  .move-row { display: grid; grid-template-columns: 28px 1fr 1fr; gap: 4px; padding: 1px 0; }
  .move-row .num { color: var(--muted); }
  .move-row.latest .wm, .move-row.latest .bm { color: var(--green); font-weight: bold; }

  .btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
  button { padding: 7px 13px; border-radius: 6px; border: 1px solid var(--border);
           background: var(--border); color: var(--text); cursor: pointer;
           font-size: 0.82rem; transition: all 0.2s; }
  button:hover { background: var(--accent); border-color: var(--accent); }
  button.primary { background: var(--accent); border-color: var(--accent); }
  button.secondary { background: var(--border); }
  button.warning { background: #664400; border-color: var(--orange); color: var(--orange); }
  button.warning:hover { background: var(--orange); color: #1a1a1a; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }

  select, input[type=text] {
    padding: 7px 10px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--border); color: var(--text); font-size: 0.82rem; cursor: pointer;
  }
  input[type=color] { width: 36px; height: 28px; padding: 2px; border-radius: 4px;
                      border: 1px solid var(--border); background: var(--border); cursor: pointer; }

  .sys-row { display: flex; justify-content: space-between; font-size: 0.78rem;
             padding: 3px 0; border-bottom: 1px solid var(--border); }
  .sys-row:last-child { border: none; }
  .sys-val { color: var(--green); font-family: monospace; }

  .status-line { padding: 8px; background: var(--border); border-radius: 6px;
                 font-size: 0.82rem; color: var(--blue); min-height: 36px;
                 display: flex; align-items: center; gap: 8px; }

  .theme-swatch { width: 20px; height: 20px; border-radius: 3px; display: inline-block;
                  border: 1px solid #555; vertical-align: middle; margin-right: 4px; }

  .move-input-row { display: flex; gap: 6px; margin-top: 6px; }
  .move-input-row input { flex: 1; text-transform: lowercase; }

  .voice-dot { width: 8px; height: 8px; border-radius: 50%; background: #555;
               display: inline-block; margin-right: 4px; }
  .voice-dot.on { background: var(--green); }

  .ota-section { margin-top: 8px; }
  .ota-output { background: #111; border-radius: 4px; padding: 8px; font-family: monospace;
                font-size: 0.75rem; max-height: 100px; overflow-y: auto; display: none;
                color: #0f0; margin-top: 6px; }

  @media (max-width: 700px) { .layout { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>♟ Smart Chess Board</h1>
  <span class="badge" id="mode-badge">—</span>
  <span class="badge" id="turn-badge">—</span>
  <span class="dot" id="conn-dot" style="background:#e94560"></span>
</header>

<div class="layout">

  <div style="display:flex;flex-direction:column;gap:12px;">
    <div class="panel" id="board-panel">
      <div id="turn-banner" class="turn-banner turn-white">White's Turn</div>
      <div id="board-svg">Loading board...</div>
      <div class="eval-bar">
        <div class="eval-fill" id="eval-fill" style="--pct:50%"></div>
      </div>
      <div class="eval-label" id="eval-label">Evaluation: even</div>
    </div>

    <div class="panel">
      <h3>Status</h3>
      <div class="status-line" id="status-line">Connecting...</div>
    </div>

    <div class="panel">
      <h3>Enter Move (from browser)</h3>
      <p style="font-size:0.78rem;color:var(--muted);margin-bottom:6px;">
        Type a UCI move (e.g. <code>e2e4</code>) to play from this screen.
      </p>
      <div class="move-input-row">
        <input type="text" id="move-input" placeholder="e2e4" maxlength="5">
        <button onclick="sendMove()" id="move-btn">Send</button>
      </div>
      <div id="move-feedback" style="font-size:0.78rem;color:var(--muted);margin-top:4px;"></div>
    </div>
  </div>

  <div class="right-col">

    <div class="panel">
      <h3>Game Info</h3>
      <div class="stat-grid">
        <div class="stat"><label>Mode</label><value id="s-mode">—</value></div>
        <div class="stat"><label>Difficulty</label><value id="s-diff">—</value></div>
        <div class="stat"><label>Moves</label><value id="s-moves">0</value></div>
        <div class="stat"><label>Turn</label><value id="s-turn">—</value></div>
      </div>
    </div>

    <div class="panel">
      <h3>Move History</h3>
      <div class="move-list" id="move-list">No moves yet.</div>
    </div>

    <div class="panel">
      <h3>Controls</h3>
      <div class="btn-row">
        <button class="primary" onclick="newGame()">⟳ New Game</button>
        <button onclick="requestHint()">💡 Hint</button>
        <button class="warning" onclick="requestUndo()">↩ Undo</button>
      </div>
      <br>
      <div style="display:flex;align-items:center;gap:8px;">
        <label style="font-size:0.8rem;color:var(--muted)">LED Theme:</label>
        <select id="theme-select" onchange="setTheme(this.value)">
          <option value="classic">Classic</option>
          <option value="fire">Fire</option>
          <option value="ocean">Ocean</option>
          <option value="forest">Forest</option>
          <option value="neon">Neon</option>
          <option value="custom">Custom…</option>
        </select>
      </div>

      <div id="custom-theme-panel" style="display:none;margin-top:10px;border-top:1px solid var(--border);padding-top:10px;">
        <p style="font-size:0.78rem;color:var(--muted);margin-bottom:8px;">Custom LED colours:</p>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:0.8rem;">
          <label>Light square <input type="color" id="c-light" value="#ffffff" oninput="previewCustom()"></label>
          <label>Dark square  <input type="color" id="c-dark"  value="#000000" oninput="previewCustom()"></label>
          <label>Move colour  <input type="color" id="c-move"  value="#00ff00" oninput="previewCustom()"></label>
          <label>Hint colour  <input type="color" id="c-hint"  value="#00ffff" oninput="previewCustom()"></label>
        </div>
        <button onclick="applyCustomTheme()" style="margin-top:8px;width:100%">Apply Custom Theme</button>
      </div>
    </div>

    <div class="panel">
      <h3>Save &amp; Export</h3>
      <div class="btn-row">
        <button id="usb-btn" onclick="saveToUsb()" disabled>💾 Save to USB</button>
        <a id="pgn-link" href="/api/pgn/download">
          <button>⬇ Download PGN</button>
        </a>
      </div>
      <div id="usb-status" style="font-size:0.78rem;color:var(--muted);margin-top:6px;"></div>
    </div>

    <div class="panel">
      <h3>Voice</h3>
      <div style="display:flex;align-items:center;gap:10px;">
        <span class="voice-dot" id="voice-dot"></span>
        <span id="voice-label" style="font-size:0.82rem;">Checking...</span>
        <button onclick="toggleVoice()" id="voice-btn" style="margin-left:auto;">Toggle</button>
      </div>
      <p style="font-size:0.72rem;color:var(--muted);margin-top:6px;">
        Plug in a USB speaker to enable audio move announcements.
      </p>
    </div>

    <div class="panel">
      <h3>Jetson System</h3>
      <div id="sys-info">
        <div class="sys-row"><span>CPU Temp</span> <span class="sys-val" id="sys-temp">—</span></div>
        <div class="sys-row"><span>CPU Usage</span><span class="sys-val" id="sys-cpu">—</span></div>
        <div class="sys-row"><span>RAM Used</span> <span class="sys-val" id="sys-ram">—</span></div>
      </div>
      <div class="ota-section">
        <button onclick="otaUpdate()" style="margin-top:8px;width:100%;font-size:0.78rem;">
          🔄 Check for Updates (git pull)
        </button>
        <div class="ota-output" id="ota-output"></div>
      </div>
    </div>

  </div>
</div>

<script>
const socket = io();

socket.on('connect', () => {
  document.getElementById('conn-dot').style.background = '#00ff88';
});
socket.on('disconnect', () => {
  document.getElementById('conn-dot').style.background = '#e94560';
});

socket.on('state_update', (d) => {
  document.getElementById('board-svg').innerHTML = d.board_svg;

  const banner = document.getElementById('turn-banner');
  banner.className = 'turn-banner ' + (d.thinking ? 'thinking' :
                     (d.whose_turn === 'White' ? 'turn-white' : 'turn-black'));
  banner.textContent = d.thinking ? '⏳ Engine thinking...' : d.whose_turn + "'s Turn";

  document.getElementById('s-mode').textContent  = d.game_mode  || '—';
  document.getElementById('s-diff').textContent  = d.difficulty || '—';
  document.getElementById('s-moves').textContent = d.move_count || 0;
  document.getElementById('s-turn').textContent  = d.whose_turn || '—';
  document.getElementById('mode-badge').textContent = d.game_mode || '—';
  document.getElementById('turn-badge').textContent = (d.whose_turn || '—') + "'s Turn";
  document.getElementById('status-line').textContent = d.status || '';

  const score = Math.max(-500, Math.min(500, d.eval_score || 0));
  const pct   = ((score + 500) / 1000 * 100).toFixed(1);
  document.getElementById('eval-fill').style.setProperty('--pct', pct + '%');
  const label = score === 0 ? 'even' :
                score > 0   ? '+' + (score/100).toFixed(2) + ' White' :
                              (score/100).toFixed(2) + ' Black';
  document.getElementById('eval-label').textContent = 'Evaluation: ' + label;

  // Move history
  const hist  = d.move_history || [];
  const pairs = [];
  for (let i = 0; i < hist.length; i += 2) {
    pairs.push({ num: i/2+1, w: hist[i], b: hist[i+1] || '' });
  }
  const ml = document.getElementById('move-list');
  if (!pairs.length) {
    ml.innerHTML = '<span style="color:var(--muted)">No moves yet.</span>';
  } else {
    ml.innerHTML = pairs.map((p, idx) =>
      `<div class="move-row ${idx===pairs.length-1?'latest':''}">
        <span class="num">${p.num}.</span>
        <span class="wm">${p.w}</span>
        <span class="bm">${p.b}</span>
      </div>`
    ).join('');
    ml.scrollTop = ml.scrollHeight;
  }

  // Theme selector sync
  const sel = document.getElementById('theme-select');
  if (d.theme !== 'custom' && sel.value !== d.theme) sel.value = d.theme;
  document.getElementById('custom-theme-panel').style.display =
    (d.theme === 'custom' || sel.value === 'custom') ? 'block' : 'none';

  // USB save button
  const usbBtn = document.getElementById('usb-btn');
  usbBtn.disabled = !d.usb_available;
  document.getElementById('usb-status').textContent = d.usb_available
    ? 'USB drive detected — ready to save.'
    : 'No USB drive detected.';

  // Voice
  const vDot = document.getElementById('voice-dot');
  const vLbl = document.getElementById('voice-label');
  vDot.className = 'voice-dot ' + (d.voice_enabled ? 'on' : '');
  vLbl.textContent = d.voice_enabled ? 'Voice: ON' : 'Voice: OFF';
});

socket.on('move_rejected', (d) => {
  document.getElementById('move-feedback').textContent = '✗ ' + (d.reason || 'Rejected');
  document.getElementById('move-feedback').style.color = '#e94560';
});
socket.on('move_accepted', () => {
  document.getElementById('move-feedback').textContent = '✓ Move sent';
  document.getElementById('move-feedback').style.color = '#00ff88';
  document.getElementById('move-input').value = '';
  setTimeout(() => { document.getElementById('move-feedback').textContent = ''; }, 2000);
});

function newGame()      { socket.emit('new_game'); }
function requestHint()  { socket.emit('hint'); }
function requestUndo()  { socket.emit('undo'); }
function saveToUsb()    { socket.emit('save_usb'); }
function toggleVoice()  { socket.emit('toggle_voice'); }

function setTheme(t) {
  if (t === 'custom') {
    document.getElementById('custom-theme-panel').style.display = 'block';
    return;
  }
  document.getElementById('custom-theme-panel').style.display = 'none';
  socket.emit('set_theme', { theme: t });
}

function previewCustom() {
  // Live preview is applied on 'Apply' click; pickers give instant visual feedback
}

function applyCustomTheme() {
  socket.emit('set_custom_theme', {
    light: document.getElementById('c-light').value,
    dark:  document.getElementById('c-dark').value,
    move:  document.getElementById('c-move').value,
    hint:  document.getElementById('c-hint').value,
  });
}

function sendMove() {
  const uci = document.getElementById('move-input').value.trim().toLowerCase();
  if (uci.length < 4) {
    document.getElementById('move-feedback').textContent = 'Enter a valid move (e.g. e2e4)';
    document.getElementById('move-feedback').style.color = '#e94560';
    return;
  }
  socket.emit('move_input', { uci });
  document.getElementById('move-feedback').textContent = '⏳ Sending...';
  document.getElementById('move-feedback').style.color = var(--blue);
}
// Allow Enter key in move input
document.getElementById('move-input')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendMove();
});

function fetchSysInfo() {
  fetch('/api/system').then(r => r.json()).then(d => {
    document.getElementById('sys-temp').textContent =
      d.cpu_temp_c !== 'N/A' ? d.cpu_temp_c + '°C' : 'N/A';
    document.getElementById('sys-cpu').textContent =
      d.cpu_pct    !== 'N/A' ? d.cpu_pct    + '%'  : 'N/A';
    document.getElementById('sys-ram').textContent =
      d.ram_used_mb !== 'N/A'
        ? d.ram_used_mb + ' / ' + d.ram_total_mb + ' MB' : 'N/A';
  }).catch(() => {});
}
fetchSysInfo();
setInterval(fetchSysInfo, 5000);

function otaUpdate() {
  const otaToken = prompt('Enter OTA update token:');
  if (!otaToken) return;
  const out = document.getElementById('ota-output');
  out.style.display = 'block';
  out.textContent   = 'Running git pull...';
  fetch('/api/update', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ token: otaToken, restart: confirm('Restart service after pull?') }),
  })
  .then(r => r.json())
  .then(d => {
    out.textContent = d.error || d.output || 'Done.';
    if (d.restarting) out.textContent += '\\n\\nRestarting service...';
  })
  .catch(e => { out.textContent = 'Error: ' + e; });
}
</script>
</body>
</html>
"""
