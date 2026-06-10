"""
web/server.py
Real-time web dashboard for the Smart Chess Board.

Access from any device on the same network:
  http://<jetson-ip>:5000

Features:
  - Live chess board with current position (SVG rendered)
  - Move history log
  - Engine thinking indicator
  - LED theme switcher
  - Game controls (new game, hint request)
  - Stockfish evaluation bar
  - System info (CPU temp, Jetson stats)

Install:
  pip install flask flask-socketio python-chess --break-system-packages

Run alongside main.py — it starts in a background thread automatically.
"""

import os
import time
import threading
import logging
import chess
import chess.svg
from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO, emit

log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "fallback_dev_key")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Shared state (written by main.py, read by dashboard) ─────────────────────
_state = {
    "fen":          chess.STARTING_FEN,
    "move_history": [],          # list of UCI strings
    "whose_turn":   "White",
    "game_mode":    "—",
    "difficulty":   0,
    "last_move":    None,        # chess.Move or None
    "status":       "Waiting to start",
    "thinking":     False,
    "theme":        "classic",
    "eval_score":   0,           # centipawns from white's perspective
    "hint_move":    "",
    "game_active":  False,
}
_state_lock = threading.Lock()

# Callback hooks set by main.py so the dashboard can trigger game actions
_callbacks = {
    "new_game":   None,
    "hint":       None,
    "set_theme":  None,
}


# ── Public API (called from main.py) ─────────────────────────────────────────

def update_state(**kwargs):
    """Thread-safe state update + push to all connected browsers."""
    with _state_lock:
        _state.update(kwargs)
    _push_state()


def register_callbacks(new_game=None, hint=None, set_theme=None):
    _callbacks["new_game"]  = new_game
    _callbacks["hint"]      = hint
    _callbacks["set_theme"] = set_theme


def start_server(host="0.0.0.0", port=5000):
    """Start the Flask-SocketIO server in a daemon thread."""
    t = threading.Thread(
        target=lambda: socketio.run(app, host=host, port=port,
                                    allow_unsafe_werkzeug=True),
        daemon=True,
        name="web-dashboard",
    )
    t.start()
    log.info(f"Web dashboard running at http://{host}:{port}")


# ── Socket push ──────────────────────────────────────────────────────────────

def _push_state():
    with _state_lock:
        board = chess.Board(_state["fen"])
        last  = _state["last_move"]
        arrows = []
        if last:
            try:
                move = chess.Move.from_uci(last)
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
            board,
            arrows=arrows,
            size=360,
            colors={
                "square light": "#f0d9b5",
                "square dark":  "#b58863",
                "square light lastmove": "#cdd16a",
                "square dark lastmove":  "#aaa23a",
            },
        )
        payload = {
            "board_svg":    svg,
            "move_history": _state["move_history"][-40:],
            "whose_turn":   _state["whose_turn"],
            "game_mode":    _state["game_mode"],
            "difficulty":   _state["difficulty"],
            "status":       _state["status"],
            "thinking":     _state["thinking"],
            "theme":        _state["theme"],
            "eval_score":   _state["eval_score"],
            "game_active":  _state["game_active"],
            "move_count":   len(_state["move_history"]),
        }

    socketio.emit("state_update", payload)


# ── Routes ───────────────────────────────────────────────────────────────────

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
    """Return Jetson system info."""
    info = {}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            info["cpu_temp_c"] = int(f.read().strip()) // 1000
    except Exception:
        info["cpu_temp_c"] = "N/A"
    try:
        import psutil
        info["cpu_pct"]  = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        info["ram_used_mb"]  = mem.used  // 1024 // 1024
        info["ram_total_mb"] = mem.total // 1024 // 1024
    except Exception:
        info["cpu_pct"] = info["ram_used_mb"] = info["ram_total_mb"] = "N/A"
    return jsonify(info)


# ── Socket events ────────────────────────────────────────────────────────────

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


@socketio.on("set_theme")
def on_set_theme(data):
    theme = data.get("theme", "classic")
    log.info(f"Theme change via dashboard: {theme}")
    update_state(theme=theme)
    if _callbacks["set_theme"]:
        _callbacks["set_theme"](theme)


# ── Dashboard HTML (single-file, no external CDN except Socket.IO) ───────────

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
    --green: #00ff88; --blue: #00ccff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif;
         min-height: 100vh; }

  header { background: var(--surface); border-bottom: 2px solid var(--border);
           padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 1.3rem; color: var(--green); }
  .badge { background: var(--border); padding: 3px 10px; border-radius: 12px;
           font-size: 0.75rem; color: var(--blue); }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green);
         animation: pulse 1.5s infinite; margin-left: auto; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

  .layout { display: grid; grid-template-columns: 1fr 360px; gap: 16px;
            padding: 16px; max-width: 1100px; margin: 0 auto; }

  .panel { background: var(--surface); border: 1px solid var(--border);
           border-radius: 10px; padding: 14px; }
  .panel h3 { color: var(--blue); font-size: 0.85rem; text-transform: uppercase;
              letter-spacing: 1px; margin-bottom: 10px; }

  #board-panel { display: flex; flex-direction: column; align-items: center; gap: 12px; }
  #board-svg svg { width: 100%; max-width: 360px; border-radius: 6px; }

  .turn-banner { width: 100%; padding: 8px; text-align: center; border-radius: 6px;
                 font-weight: bold; font-size: 0.95rem; transition: all 0.3s; }
  .turn-white { background: #f0d9b5; color: #1a1a1a; }
  .turn-black { background: #1a1a1a; color: #f0d9b5; border: 1px solid #444; }
  .thinking { background: #0f3460 !important; color: var(--blue) !important;
              animation: blink 0.8s infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.5} }

  .eval-bar { width: 100%; height: 14px; background: #333; border-radius: 7px;
              overflow: hidden; position: relative; }
  .eval-fill { height: 100%; background: linear-gradient(90deg, #fff 0%, #fff var(--pct), #222 var(--pct), #222 100%);
               transition: width 0.5s; }
  .eval-label { text-align: center; font-size: 0.72rem; color: var(--muted); margin-top: 3px; }

  /* Right column */
  .right-col { display: flex; flex-direction: column; gap: 12px; }

  .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .stat { background: var(--border); border-radius: 6px; padding: 8px 10px; }
  .stat label { font-size: 0.7rem; color: var(--muted); display: block; }
  .stat value { font-size: 1.1rem; font-weight: bold; color: var(--green); }

  .move-list { max-height: 220px; overflow-y: auto; font-family: monospace;
               font-size: 0.82rem; line-height: 1.6; }
  .move-list::-webkit-scrollbar { width: 4px; }
  .move-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  .move-row { display: grid; grid-template-columns: 28px 1fr 1fr; gap: 4px; padding: 1px 0; }
  .move-row .num { color: var(--muted); }
  .move-row .wm  { color: var(--text); }
  .move-row .bm  { color: #aaa; }
  .move-row.latest .wm, .move-row.latest .bm { color: var(--green); font-weight: bold; }

  .btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
  button { padding: 8px 14px; border-radius: 6px; border: 1px solid var(--border);
           background: var(--border); color: var(--text); cursor: pointer;
           font-size: 0.82rem; transition: all 0.2s; }
  button:hover { background: var(--accent); border-color: var(--accent); }
  button.primary { background: var(--accent); border-color: var(--accent); }

  select { padding: 7px 10px; border-radius: 6px; border: 1px solid var(--border);
           background: var(--border); color: var(--text); font-size: 0.82rem;
           cursor: pointer; }

  .sys-row { display: flex; justify-content: space-between; font-size: 0.78rem;
             padding: 3px 0; border-bottom: 1px solid var(--border); }
  .sys-row:last-child { border: none; }
  .sys-val { color: var(--green); font-family: monospace; }

  .status-line { padding: 8px; background: var(--border); border-radius: 6px;
                 font-size: 0.82rem; color: var(--blue); min-height: 36px;
                 display: flex; align-items: center; gap: 8px; }

  @media (max-width: 700px) {
    .layout { grid-template-columns: 1fr; }
  }
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

  <!-- Left: board + eval -->
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
  </div>

  <!-- Right: stats, history, controls -->
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
      </div>
      <br>
      <div style="display:flex;align-items:center;gap:8px;margin-top:4px;">
        <label style="font-size:0.8rem;color:var(--muted)">LED Theme:</label>
        <select id="theme-select" onchange="setTheme(this.value)">
          <option value="classic">Classic</option>
          <option value="fire">Fire</option>
          <option value="ocean">Ocean</option>
          <option value="forest">Forest</option>
          <option value="neon">Neon</option>
        </select>
      </div>
    </div>

    <div class="panel">
      <h3>Jetson System</h3>
      <div id="sys-info">
        <div class="sys-row"><span>CPU Temp</span><span class="sys-val" id="sys-temp">—</span></div>
        <div class="sys-row"><span>CPU Usage</span><span class="sys-val" id="sys-cpu">—</span></div>
        <div class="sys-row"><span>RAM Used</span><span class="sys-val" id="sys-ram">—</span></div>
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
  // Board SVG
  document.getElementById('board-svg').innerHTML = d.board_svg;

  // Turn banner
  const banner = document.getElementById('turn-banner');
  banner.className = 'turn-banner ' + (d.thinking ? 'thinking' :
                     (d.whose_turn === 'White' ? 'turn-white' : 'turn-black'));
  banner.textContent = d.thinking ? '⏳ Engine thinking...' : d.whose_turn + "'s Turn";

  // Stats
  document.getElementById('s-mode').textContent  = d.game_mode  || '—';
  document.getElementById('s-diff').textContent  = d.difficulty || '—';
  document.getElementById('s-moves').textContent = d.move_count || 0;
  document.getElementById('s-turn').textContent  = d.whose_turn || '—';
  document.getElementById('mode-badge').textContent = d.game_mode || '—';
  document.getElementById('turn-badge').textContent = d.whose_turn + "'s Turn";

  // Status
  document.getElementById('status-line').textContent = d.status || '';

  // Eval bar
  const score = Math.max(-500, Math.min(500, d.eval_score || 0));
  const pct   = ((score + 500) / 1000 * 100).toFixed(1);
  document.getElementById('eval-fill').style.setProperty('--pct', pct + '%');
  const label = score === 0 ? 'even' :
                score > 0   ? `+${(score/100).toFixed(2)} White` :
                              `${(score/100).toFixed(2)} Black`;
  document.getElementById('eval-label').textContent = 'Evaluation: ' + label;

  // Move history
  const hist = d.move_history || [];
  const pairs = [];
  for (let i = 0; i < hist.length; i += 2) {
    pairs.push({ num: i/2+1, w: hist[i], b: hist[i+1] || '' });
  }
  const ml = document.getElementById('move-list');
  if (pairs.length === 0) {
    ml.innerHTML = '<span style="color:var(--muted)">No moves yet.</span>';
  } else {
    ml.innerHTML = pairs.map((p, idx) =>
      `<div class="move-row ${idx === pairs.length-1 ? 'latest' : ''}">
        <span class="num">${p.num}.</span>
        <span class="wm">${p.w}</span>
        <span class="bm">${p.b}</span>
      </div>`
    ).join('');
    ml.scrollTop = ml.scrollHeight;
  }

  // Theme selector sync
  const sel = document.getElementById('theme-select');
  if (sel.value !== d.theme) sel.value = d.theme;
});

// System info poll every 5s
function fetchSysInfo() {
  fetch('/api/system').then(r => r.json()).then(d => {
    document.getElementById('sys-temp').textContent =
      d.cpu_temp_c !== 'N/A' ? d.cpu_temp_c + '°C' : 'N/A';
    document.getElementById('sys-cpu').textContent =
      d.cpu_pct !== 'N/A' ? d.cpu_pct + '%' : 'N/A';
    document.getElementById('sys-ram').textContent =
      d.ram_used_mb !== 'N/A' ? d.ram_used_mb + ' / ' + d.ram_total_mb + ' MB' : 'N/A';
  }).catch(() => {});
}
fetchSysInfo();
setInterval(fetchSysInfo, 5000);

function newGame()   { socket.emit('new_game'); }
function requestHint(){ socket.emit('hint'); }
function setTheme(t) { socket.emit('set_theme', { theme: t }); }
</script>
</body>
</html>
"""
