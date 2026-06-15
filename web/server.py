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
from storage import usb

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
    "usb_games":        [],
    "voice_enabled":    False,
    "setup_phase":      "idle",    # idle | mode_select | difficulty | time | colour | ready
    "web_player":       None,      # None | "White" | "Black" — which side web controls in LocalHuman
    "disco_active":     False,
    "led_grid":         [[0,0,0]]*64,
    "draw_reason":      "",
    "oled_lines":       ["Starting setup...", "", "", ""],  # Mirrors physical row states
    "needs_ok":         False,
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
    "web_mode_select":    None,
    "web_setup_answer":   None,
    "slider_preview":     None,
    "disco":              None,
    "load_usb_game":      None,
    "web_ok":             None,
    "set_brightness":     None,
    "viewer_board_reset": None,
    "viewer_board_goto":  None,
    "evaluate_fen":       None,   # called with (fen) -> int centipawns
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


def show_ok_banner(move_str: str):
    """Emit a dedicated event — bypasses _push_state so it arrives immediately."""
    socketio.emit("ok_banner_show", {"move": move_str})

def hide_ok_banner():
    """Emit hide event directly."""
    socketio.emit("ok_banner_hide", {})

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
            "usb_games":     _state["usb_games"],
            "needs_ok":      _state["needs_ok"],
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


@app.route("/api/pgn/upload", methods=["POST"])
def api_pgn_upload():
    """
    Accept a PGN file upload from the browser and save it to the USB drive.
    Supports multipart/form-data (file input) or raw text body.
    """
    pgn_text = None
    source_name = "uploaded"

    # Multipart file upload
    if "file" in request.files:
        f = request.files["file"]
        source_name = f.filename.replace(".pgn", "") if f.filename else "uploaded"
        pgn_text = f.read().decode("utf-8", errors="replace")
    # Raw body (text/plain or application/x-chess-pgn)
    elif request.data:
        pgn_text = request.data.decode("utf-8", errors="replace")
        source_name = request.args.get("name", "uploaded")

    if not pgn_text or not pgn_text.strip():
        return jsonify({"error": "No PGN data received"}), 400

    filename = usb.import_pgn(pgn_text, source_name)
    if not filename:
        return jsonify({"error": "Import failed — check USB drive is connected and writeable"}), 500

    # Refresh the games list in state
    games = usb.list_saved_games()
    update_state(usb_games=games)

    return jsonify({"ok": True, "filename": filename})


# ── Socket events ──────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    sid = getattr(request, "sid", "unknown")
    log.info(f"Dashboard client connected: {sid}")
    # Refresh USB state live on each new connection so a page reload
    # always shows the correct drive status without waiting for the monitor
    usb_ok = usb.is_available()
    with _state_lock:
        _state["usb_available"] = usb_ok
        if usb_ok:
            _state["usb_games"] = usb.list_saved_games()
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

@socketio.on("web_ok")
def on_web_ok():
    log.info("Web OK received")
    if _callbacks["web_ok"]:
        threading.Thread(target=_callbacks["web_ok"], daemon=True).start()

@socketio.on("set_brightness_live")
def on_set_brightness_live(data):
    """Live brightness preview from dev panel slider."""
    val = int(data.get("value", 76))
    cb = _callbacks.get("set_brightness")
    if cb:
        threading.Thread(target=cb, args=(val,), daemon=True).start()

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

    # Apply brightness live to hardware if changed
    if "chess_led_brightness" in data:
        cb = _callbacks.get("set_brightness")
        if cb:
            threading.Thread(target=cb, args=(CFG.chess_led_brightness,), daemon=True).start()

    log.info("Dev settings applied: " + ", ".join(changed))
    emit("dev_settings_saved", {
        "ok":      True,
        "changed": changed,
        "note":    "Settings are live for this session. Restart to revert.",
    })

@socketio.on("request_usb_games")
def handle_request_usb_games():
    if usb.is_available():
        games = usb.list_saved_games()
        emit("usb_games_list", {"available": True, "games": games})
    else:
        emit("usb_games_list", {"available": False, "games": []})


@socketio.on("delete_usb_game")
def handle_delete_usb_game(data):
    """Delete a named PGN from the USB drive and push the refreshed list."""
    filename = data.get("filename", "")
    ok = usb.delete_game(filename)
    if ok:
        games = usb.list_saved_games()
        update_state(usb_games=games)
        emit("delete_usb_game_result", {"ok": True, "filename": filename, "games": games})
    else:
        emit("delete_usb_game_result", {"ok": False, "filename": filename, "error": "Delete failed"})

@socketio.on("trigger_load_game")
def handle_trigger_load_game(data):
    """
    Fires when a user clicks a specific game file from their browser window.
    """
    filename = data.get("filename")
    if not filename:
        return
    log.info(f"Web requested loading match file: {filename}")
    if _callbacks["load_usb_game"]:
        import threading
        threading.Thread(target=_callbacks["load_usb_game"], args=(filename,), daemon=True).start()


@socketio.on("viewer_load_game")
def handle_viewer_load_game(data):
    """
    Load a PGN for the in-browser game viewer.
    Builds FEN + Stockfish eval at every half-move, emits back the full list.
    Evals are computed quickly (depth 10) so this may take a few seconds.
    """
    filename = data.get("filename")
    if not filename:
        emit("viewer_game_data", {"ok": False, "error": "No filename"})
        return

    mount = usb.get_active_mount()
    if not mount:
        emit("viewer_game_data", {"ok": False, "error": "No USB drive"})
        return

    import os as _os
    full_path = _os.path.join(mount, usb.subdir, filename)
    if not _os.path.exists(full_path):
        emit("viewer_game_data", {"ok": False, "error": "File not found"})
        return

    try:
        import chess.pgn as _pgn
        with open(full_path, "r", encoding="utf-8") as f:
            game = _pgn.read_game(f)
        if not game:
            emit("viewer_game_data", {"ok": False, "error": "Could not parse PGN"})
            return

        headers = game.headers
        moves   = [m.uci() for m in game.mainline_moves()]

        # Build FEN at every position (0=start, N=after N moves)
        board = chess.Board()
        fens  = [board.fen()]
        for uci in moves:
            try:
                board.push(chess.Move.from_uci(uci))
            except Exception:
                break
            fens.append(board.fen())

        # Compute Stockfish evals for every position.
        # Prefer the shared engine instance via callback to avoid spawning a
        # second Stockfish process (which would conflict with the running game).
        evals = []
        eval_cb = _callbacks.get("evaluate_fen")

        if eval_cb:
            # Use the game's existing StockfishEngine instance
            total_fens = len(fens)
            for idx_f, fen in enumerate(fens):
                try:
                    evals.append(int(eval_cb(fen)))
                except Exception:
                    evals.append(0)
                # Stream progress every 10 positions so client can show a bar
                if idx_f % 10 == 0:
                    socketio.emit("viewer_eval_progress", {
                        "done": idx_f, "total": total_fens
                    })
        else:
            # Fallback: spawn a short-lived engine (only works if no game running)
            try:
                import chess.engine as _eng
                from config import CFG as _CFG
                _sf = _eng.SimpleEngine.popen_uci(_CFG.stockfish_path)
                for fen in fens:
                    try:
                        b = chess.Board(fen)
                        info = _sf.analyse(b, _eng.Limit(depth=10))
                        score_obj = info.get("score")
                        if score_obj is not None:
                            sc = score_obj.white().score(mate_score=10000)
                            evals.append(sc if sc is not None else 0)
                        else:
                            evals.append(0)
                    except Exception:
                        evals.append(0)
                _sf.quit()
            except Exception as e:
                log.warning(f"Viewer fallback eval failed: {e}")
                evals = [0] * len(fens)

        emit("viewer_game_data", {
            "ok":     True,
            "moves":  moves,
            "fens":   fens,
            "evals":  evals,
            "white":  headers.get("White",  "White"),
            "black":  headers.get("Black",  "Black"),
            "date":   headers.get("Date",   "?"),
            "event":  headers.get("Event",  "Game"),
            "result": headers.get("Result", "*"),
        })
    except Exception as e:
        log.error(f"viewer_load_game error: {e}")
        emit("viewer_game_data", {"ok": False, "error": str(e)})


# Track active board-replay session
_viewer_session = {"moves": [], "fens": [], "pos": 0, "active": False}
_viewer_lock = threading.Lock()

@socketio.on("viewer_board_load")
def handle_viewer_board_load(data):
    """
    Load a game into the board-replay session without fast-forwarding.
    The dashboard then drives it move by move via viewer_board_step.
    """
    filename = data.get("filename")
    if not filename:
        return
    moves = usb.load_game(filename)
    if moves is None:
        emit("viewer_board_status", {"error": "Could not load game"})
        return
    with _viewer_lock:
        _viewer_session["moves"]  = moves
        _viewer_session["fens"]   = []
        _viewer_session["pos"]    = 0
        _viewer_session["active"] = True
    # Reset board on hardware via callback
    if _callbacks.get("load_usb_game"):
        # Build the initial empty state (no moves played)
        emit("viewer_board_status", {"ok": True, "total": len(moves), "pos": 0})
        threading.Thread(
            target=_do_viewer_board_reset,
            args=(moves,),
            daemon=True,
        ).start()

def _do_viewer_board_reset(moves):
    """Reset hardware board to start position for step-replay."""
    cb = _callbacks.get("viewer_board_reset")
    if cb:
        cb(moves)

@socketio.on("viewer_board_step")
def handle_viewer_board_step(data):
    """Step the physical board replay forward or backward by delta."""
    delta = int(data.get("delta", 1))
    with _viewer_lock:
        if not _viewer_session["active"]:
            return
        moves = _viewer_session["moves"]
        pos   = _viewer_session["pos"]
        new_pos = max(0, min(len(moves), pos + delta))
        _viewer_session["pos"] = new_pos
    cb = _callbacks.get("viewer_board_goto")
    if cb:
        threading.Thread(target=cb, args=(new_pos,), daemon=True).start()
    emit("viewer_board_status", {"ok": True, "total": len(moves), "pos": new_pos})

@socketio.on("viewer_board_goto")
def handle_viewer_board_goto(data):
    """Jump the physical board replay to an absolute position."""
    pos = int(data.get("pos", 0))
    with _viewer_lock:
        if not _viewer_session["active"]:
            return
        moves = _viewer_session["moves"]
        pos = max(0, min(len(moves), pos))
        _viewer_session["pos"] = pos
    cb = _callbacks.get("viewer_board_goto")
    if cb:
        threading.Thread(target=cb, args=(pos,), daemon=True).start()
    emit("viewer_board_status", {"ok": True, "total": len(moves), "pos": pos})

@socketio.on("viewer_board_stop")
def handle_viewer_board_stop(data):
    with _viewer_lock:
        _viewer_session["active"] = False
    log.info("Board viewer replay stopped")


@socketio.on("switch_project")
def handle_switch_project(data):
    """
    Cleanly stop the chess service and start another systemd service.
    The target service name is looked up from environment variables so
    it can be configured in .env without changing code.

    .env keys:
      SWITCH_SQUID_SERVICE=squidgame.service   (name of the other project's service)
      SWITCH_CHESS_SERVICE=chess.service        (this service — used when switching back)

    Add more projects by adding SWITCH_<TARGET>_SERVICE=<name>.service to .env
    """
    target = (data.get("target") or "").strip().lower()
    if not target:
        emit("switch_project_status", {"ok": False, "error": "No target specified"})
        return

    # Look up the service name from environment
    env_key = f"SWITCH_{target.upper()}_SERVICE"
    service = os.environ.get(env_key, "").strip()

    if not service:
        emit("switch_project_status", {
            "ok": False,
            "error": f"{env_key} not set in .env — add it and restart chess"
        })
        return

    log.info(f"[SWITCH] Switching to {target} ({service})")

    # Push status before we lose the socket connection
    emit("switch_project_status", {
        "ok": True,
        "message": f"Starting {service}..."
    })

    # Run the switch in a thread so the emit above can flush first
    def _do_switch():
        import time as _time
        _time.sleep(0.3)   # let the emit reach the browser
        chess_service = os.environ.get("SWITCH_CHESS_SERVICE", "chess.service")
        ret = os.system(
            f"sudo systemctl stop {chess_service} && sudo systemctl start {service}"
        )
        if ret != 0:
            log.error(f"[SWITCH] systemctl returned {ret}")

    threading.Thread(target=_do_switch, daemon=True).start()


# ── Dashboard HTML ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chess Board — Control</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
:root {
  --gold:      #c9a84c;
  --gold-dim:  #9a7a30;
  --gold-glow: rgba(201,168,76,0.12);
  --bg:        #0d0f14;
  --surface:   #161920;
  --surface2:  #1e2230;
  --surface3:  #252a38;
  --border:    #2a2f40;
  --border2:   #353b50;
  --text:      #e4e8f0;
  --text-dim:  #7a8299;
  --text-muted:#4a5068;
  --green:     #3dca6a;
  --red:       #f05050;
  --blue:      #4fb8f7;
  --orange:    #f09040;
  --r-sm:5px; --r-md:9px; --r-lg:14px; --r-xl:20px;
}
[data-theme="light"] {
  --bg:#f0f2f7; --surface:#ffffff; --surface2:#eaecf4; --surface3:#e0e4ef;
  --border:#d0d6e8; --border2:#b8c0d8; --text:#1a1d2a; --text-dim:#5a6278; --text-muted:#8890a8;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden;font-size:14px}

/* ── Topbar ── */
.topbar{display:flex;align-items:center;justify-content:space-between;padding:0 20px;height:52px;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;backdrop-filter:blur(8px)}
.topbar-left{display:flex;align-items:center;gap:14px}
.logo{font-size:15px;font-weight:700;color:var(--gold);letter-spacing:.4px;display:flex;align-items:center;gap:6px}
.conn-pill{display:flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;background:rgba(244,80,80,.1);border:1px solid rgba(244,80,80,.3);font-size:11px;color:var(--red);transition:.3s}
.conn-pill.on{background:rgba(61,202,106,.1);border-color:rgba(61,202,106,.3);color:var(--green)}
.conn-dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.topbar-right{display:flex;align-items:center;gap:6px}
.icon-btn{background:none;border:1px solid var(--border);border-radius:var(--r-md);padding:6px 12px;cursor:pointer;color:var(--text-dim);font-size:12px;font-weight:500;transition:.15s;display:flex;align-items:center;gap:5px;font-family:'Inter',sans-serif}
.icon-btn:hover{border-color:var(--gold);color:var(--gold);background:var(--gold-glow)}
.icon-btn.active-dev{border-color:var(--gold)!important;color:var(--gold)!important;background:var(--gold-glow)!important}

/* ── Layout ── */
.main{display:grid;grid-template-columns:1fr 330px;gap:14px;padding:14px;max-width:1180px;margin:0 auto}
@media(max-width:860px){.main{grid-template-columns:1fr}}
.diag-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
@media(max-width:560px){.diag-row{grid-template-columns:1fr}}

/* ── Cards ── */
.card{background:var(--surface);border-radius:var(--r-lg);padding:14px 16px;border:1px solid var(--border)}
.card-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.9px;color:var(--text-muted);margin-bottom:11px}

/* ── Board ── */
.board-wrap{background:var(--surface);border-radius:var(--r-xl);padding:14px;border:1px solid var(--border)}
.board-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:11px}
.turn-badge{padding:4px 13px;border-radius:20px;font-size:12px;font-weight:600;background:var(--gold-glow);border:1px solid var(--gold-dim);color:var(--gold)}
.mode-label{font-size:11px;color:var(--text-muted)}
.board-container{position:relative;display:flex;justify-content:center}
canvas{border-radius:var(--r-md);cursor:pointer;max-width:100%;touch-action:none;display:block}
.board-footer{margin-top:10px;display:flex;gap:7px;flex-wrap:wrap}

/* ── Setup overlay ── */
.setup-panel{display:none;position:absolute;inset:0;background:rgba(13,15,20,.94);border-radius:var(--r-md);flex-direction:column;align-items:center;justify-content:center;gap:14px;z-index:10;padding:20px;backdrop-filter:blur(4px)}
.setup-panel.show{display:flex}
.setup-title{font-size:17px;font-weight:700;color:var(--gold)}
.setup-sub{font-size:12px;color:var(--text-dim);text-align:center;max-width:250px}
.setup-btns{display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
.setup-btn{padding:9px 18px;border-radius:var(--r-md);border:1px solid var(--border2);background:var(--surface2);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;font-weight:500;cursor:pointer;transition:.15s}
.setup-btn:hover,.setup-btn.active{border-color:var(--gold);color:var(--gold);background:var(--gold-glow)}
.setup-slider{width:100%;max-width:240px;accent-color:var(--gold)}
.setup-slider-val{font-size:28px;font-weight:700;color:var(--gold)}

/* ── Web OK banner ── */
.ok-banner{display:none;background:rgba(201,168,76,.08);border:1px solid rgba(201,168,76,.3);border-radius:var(--r-md);padding:10px 14px;margin-top:10px;align-items:center;justify-content:space-between;gap:10px}
.ok-banner.show{display:flex}
.ok-banner-text{font-size:13px;color:var(--gold)}
.ok-banner-btn{padding:7px 16px;border-radius:var(--r-md);background:var(--gold);color:#111;font-weight:700;font-size:13px;cursor:pointer;border:none;font-family:'Inter',sans-serif;transition:.15s}
.ok-banner-btn:hover{background:var(--gold-dim)}

/* ── Sidebar ── */
.sidebar{display:flex;flex-direction:column;gap:10px}

/* ── Status ── */
.status-text{font-size:13px;color:var(--text);line-height:1.5;min-height:18px}
.eval-wrap{margin-top:8px;display:flex;align-items:center;gap:8px}
.eval-bar-bg{flex:1;height:5px;border-radius:3px;background:var(--surface3);overflow:hidden}
.eval-bar{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--gold-dim),var(--gold));transition:width .4s}
.eval-label{font-size:10px;color:var(--text-muted);font-family:'JetBrains Mono',monospace;min-width:34px;text-align:right}

/* ── Buttons ── */
.ctrl-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.ctrl-btn{padding:9px 8px;border-radius:var(--r-md);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-family:'Inter',sans-serif;font-size:12px;font-weight:500;cursor:pointer;transition:.15s;display:flex;align-items:center;justify-content:center;gap:4px}
.ctrl-btn:hover{border-color:var(--gold);color:var(--gold)}
.ctrl-btn.primary{background:var(--gold);border-color:var(--gold);color:#111;font-weight:700}
.ctrl-btn.primary:hover{background:var(--gold-dim)}
.ctrl-btn.danger{border-color:var(--red);color:var(--red)}
.ctrl-btn.danger:hover{background:rgba(240,80,80,.08)}
.ctrl-btn:disabled{opacity:.3;cursor:not-allowed;pointer-events:none}
.ctrl-btn.full{grid-column:1/-1}

/* ── Toggle ── */
.toggle-row{display:flex;align-items:center;justify-content:space-between;padding:3px 0}
.toggle-label{font-size:12px;color:var(--text)}
label.sw{position:relative;display:inline-block;width:36px;height:20px;flex-shrink:0}
label.sw input{opacity:0;width:0;height:0}
.sw-track{position:absolute;inset:0;background:var(--border2);border-radius:10px;transition:.25s;cursor:pointer}
.sw-track:before{content:'';position:absolute;width:14px;height:14px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.25s}
input:checked+.sw-track{background:var(--gold)}
input:checked+.sw-track:before{transform:translateX(16px)}

/* ── USB card ── */
.usb-status{font-size:11px;margin-top:7px;padding:6px 10px;border-radius:var(--r-sm);background:var(--surface3)}
.usb-status.ok{color:var(--green);border:1px solid rgba(61,202,106,.2)}
.usb-status.no{color:var(--text-muted);border:1px solid var(--border)}
.usb-games-list{margin-top:8px;display:none;flex-direction:column;gap:4px;max-height:140px;overflow-y:auto}
.usb-games-list.show{display:flex}
.usb-game-row{display:flex;align-items:center;justify-content:space-between;padding:6px 9px;border-radius:var(--r-sm);background:var(--surface3);border:1px solid var(--border);gap:8px}
.usb-game-name{font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.usb-game-btn{padding:3px 9px;border-radius:var(--r-sm);background:var(--gold-glow);border:1px solid var(--gold-dim);color:var(--gold);font-size:10px;font-weight:600;cursor:pointer;flex-shrink:0;font-family:'Inter',sans-serif;transition:.15s}
.usb-game-btn:hover{background:var(--gold);color:#111}

/* ── Move history ── */
.history-list{max-height:140px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:11px;display:grid;grid-template-columns:auto 1fr 1fr;gap:2px 6px}
.mv-num{color:var(--text-muted)}
.mv-w{color:var(--text)}
.mv-b{color:var(--text-dim)}
.mv-w.last,.mv-b.last{color:var(--gold);font-weight:700}

/* ── Theme ── */
.theme-pills{display:flex;flex-wrap:wrap;gap:5px}
.theme-pill{padding:4px 11px;border-radius:20px;border:1px solid var(--border);background:var(--surface2);color:var(--text-dim);font-size:11px;cursor:pointer;transition:.15s;font-weight:500}
.theme-pill:hover,.theme-pill.active{border-color:var(--gold);color:var(--gold)}

/* ── OLED ── */
.oled-wrap{background:#000;border:1px solid #222;border-radius:var(--r-md);padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:12px;color:#4af;text-shadow:0 0 5px rgba(64,170,255,.5);min-height:82px;display:flex;flex-direction:column;justify-content:space-between}
.oled-row{min-height:15px;white-space:pre;overflow:hidden;text-overflow:ellipsis}
.oled-row.header{background:#4af;color:#000;text-shadow:none;padding:1px 4px;border-radius:2px;font-weight:700}

/* ── LED Grid ── */
.led-grid{display:grid;grid-template-columns:repeat(8,1fr);gap:2px;background:var(--surface3);border-radius:var(--r-md);padding:4px}
.led-cell{aspect-ratio:1;border-radius:2px;background:#0a0a0a}

/* ── Overlays ── */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);display:flex;align-items:center;justify-content:center;z-index:200;opacity:0;pointer-events:none;transition:opacity .2s;padding:16px}
.overlay.show{opacity:1;pointer-events:all}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);padding:24px;width:100%;max-width:420px;position:relative;max-height:88vh;overflow-y:auto}
.modal-close{position:absolute;top:14px;right:16px;background:none;border:none;color:var(--text-dim);font-size:18px;cursor:pointer}
.modal-close:hover{color:var(--text)}
.modal h2{font-size:16px;font-weight:700;margin-bottom:14px;color:var(--gold)}
.field{margin-bottom:10px}
.field label{display:block;font-size:11px;color:var(--text-dim);margin-bottom:3px}
.field input,.field select{width:100%;padding:7px 9px;border-radius:var(--r-sm);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-family:'Inter',sans-serif;font-size:13px}
.field input:focus,.field select:focus{outline:none;border-color:var(--gold)}

/* ── PIN ── */
.pin-wrap{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);padding:24px;text-align:center;width:100%;max-width:280px}
.pin-title{font-size:15px;font-weight:700;margin-bottom:16px;color:var(--text)}
.pin-dots{display:flex;gap:10px;justify-content:center;margin-bottom:14px}
.pd{width:13px;height:13px;border-radius:50%;border:2px solid var(--border);background:transparent;transition:.2s}
.pd.filled{background:var(--gold);border-color:var(--gold)}
.pin-pad{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.pin-key{padding:13px;border-radius:var(--r-md);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-size:16px;font-weight:700;cursor:pointer;transition:.15s;font-family:'Inter',sans-serif}
.pin-key:hover{border-color:var(--gold);color:var(--gold)}
.pin-err{color:var(--red);font-size:11px;min-height:16px;margin-top:6px}

/* ── Dev panel ── */
.dev-modal{max-width:520px}
.dev-tabs{display:flex;gap:4px;margin-bottom:14px;border-bottom:1px solid var(--border);padding-bottom:0}
.dev-tab{padding:7px 14px;cursor:pointer;font-size:12px;font-weight:600;color:var(--text-dim);border-bottom:2px solid transparent;transition:.15s;margin-bottom:-1px}
.dev-tab.active{color:var(--gold);border-bottom-color:var(--gold)}
.dev-tab-panel{display:none}
.dev-tab-panel.active{display:block}
.dev-section{margin-bottom:16px}
.dev-section-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--text-muted);margin-bottom:8px;display:flex;align-items:center;gap:6px}
.dev-section-title::after{content:'';flex:1;height:1px;background:var(--border)}
.dev-row{display:grid;grid-template-columns:1fr auto;align-items:center;gap:10px;margin-bottom:7px}
.dev-row label{font-size:12px;color:var(--text-dim)}
.dev-input{width:90px;padding:5px 8px;border-radius:var(--r-sm);border:1px solid var(--border);background:var(--surface3);color:var(--text);font-size:12px;text-align:right;font-family:'JetBrains Mono',monospace}
.dev-input:focus{outline:none;border-color:var(--gold)}
.dev-input.wide{width:160px;text-align:left}
.dev-slider-row{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.dev-slider-row label{font-size:12px;color:var(--text-dim);min-width:140px}
.dev-slider{flex:1;accent-color:var(--gold)}
.dev-slider-val{font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--gold);min-width:35px;text-align:right}
.dev-save-msg{font-size:11px;min-height:16px;margin-top:8px;text-align:center}
.warn-badge{font-size:9px;padding:2px 6px;border-radius:20px;background:rgba(240,144,64,.12);border:1px solid rgba(240,144,64,.3);color:var(--orange);vertical-align:middle;margin-left:4px}

/* ── About ── */
.about-chips{display:flex;flex-wrap:wrap;gap:5px;margin:12px 0}
.chip{padding:3px 9px;border-radius:20px;border:1px solid var(--border);font-size:10px;color:var(--text-dim)}
.heart{display:inline-block;animation:hb .8s ease-in-out infinite}
@keyframes hb{0%,100%{transform:scale(1)}50%{transform:scale(1.25)}}

/* ── Custom theme ── */
.picker-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.picker-card{background:var(--surface2);border:1px solid var(--border);border-radius:var(--r-md);padding:10px;display:flex;align-items:center;gap:10px;cursor:pointer;transition:.15s}
.picker-card:hover{border-color:var(--gold)}
.picker-card input[type="color"]{-webkit-appearance:none;border:2px solid var(--border);border-radius:50%;width:34px;height:34px;cursor:pointer;background:none;padding:0;flex-shrink:0}
.picker-card input[type="color"]::-webkit-color-swatch-wrapper{padding:0}
.picker-card input[type="color"]::-webkit-color-swatch{border:none;border-radius:50%}
.picker-info{display:flex;flex-direction:column;gap:1px}
.picker-label{font-size:12px;font-weight:500}
.picker-sub{font-size:10px;color:var(--text-dim)}
.theme-note{background:var(--gold-glow);border:1px solid rgba(201,168,76,.2);border-radius:var(--r-md);padding:9px 12px;font-size:11px;color:var(--gold);margin-top:6px}

/* ── Disco ── */
.disco-hint{font-size:10px;color:var(--text-muted);text-align:center;margin-top:4px;cursor:pointer;opacity:.5;transition:.2s}
.disco-hint:hover{opacity:1;color:var(--gold)}
@keyframes disco-flash{0%{filter:hue-rotate(0deg)brightness(1.4)}100%{filter:hue-rotate(360deg)brightness(1.4)}}
.disco-active canvas{animation:disco-flash .25s linear infinite}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:3px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
/* ── Project switcher button ── */
.switch-btn{background:rgba(61,202,106,.1)!important;border-color:rgba(61,202,106,.4)!important;color:var(--green)!important;font-weight:600}
.switch-btn:hover{background:rgba(61,202,106,.2)!important;border-color:var(--green)!important}
/* ── Viewer nav buttons ── */
.vbtn{padding:7px 10px;border-radius:var(--r-sm);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-size:14px;cursor:pointer;transition:.15s;font-family:'Inter',sans-serif;line-height:1}
.vbtn:hover{border-color:var(--gold);color:var(--gold)}
/* ── Viewer move list ── */
.vml-move{background:none;border:1px solid transparent;border-radius:4px;padding:3px 6px;font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--text-dim);cursor:pointer;text-align:left;transition:.1s;width:100%}
.vml-move:hover{background:var(--surface3);color:var(--text);border-color:var(--border)}
.vml-move.vml-active{background:var(--gold-glow);color:var(--gold);border-color:var(--gold-dim);font-weight:700}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <span class="logo">♟ Chess Board</span>
    <div class="conn-pill" id="conn-pill"><div class="conn-dot"></div><span id="conn-txt">Connecting</span></div>
  </div>
  <div class="topbar-right">
    <button class="icon-btn switch-btn" onclick="switchProject('squid')" title="Switch to Squid Game project">
      🟢 Squid Game
    </button>
    <button class="icon-btn" onclick="toggleTheme()">🌓</button>
    <button class="icon-btn" id="dev-btn" onclick="openDev()">🔒 Dev</button>
    <button class="icon-btn" onclick="showAbout()">v3.1</button>
  </div>
</div>

<!-- Project switch overlay -->
<div class="overlay" id="ov-switch">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);padding:32px 28px;text-align:center;max-width:320px;width:100%">
    <div style="font-size:36px;margin-bottom:12px" id="switch-icon">🟢</div>
    <div style="font-size:16px;font-weight:700;color:var(--gold);margin-bottom:8px" id="switch-title">Switching project...</div>
    <div style="font-size:13px;color:var(--text-dim);margin-bottom:20px" id="switch-msg">Shutting down chess board safely</div>
    <div style="height:4px;border-radius:2px;background:var(--surface3);overflow:hidden">
      <div id="switch-bar" style="height:100%;width:0%;background:var(--green);border-radius:2px;transition:width .4s ease"></div>
    </div>
  </div>
</div>

<div class="main">
  <div>
    <div class="board-wrap">
      <div class="board-header">
        <div class="turn-badge" id="turn-badge">Waiting...</div>
        <div class="mode-label" id="mode-label">—</div>
      </div>
      <div class="board-container" id="board-container">
        <div class="setup-panel" id="setup-panel">
          <div class="setup-title" id="setup-title">Select Game Mode</div>
          <div class="setup-sub" id="setup-sub"></div>
          <div class="setup-btns" id="setup-btns"></div>
          <div id="setup-slider-wrap" style="display:none;width:100%;align-items:center;flex-direction:column;gap:10px">
            <div class="setup-slider-val" id="setup-slider-val">—</div>
            <input type="range" class="setup-slider" id="setup-slider" min="1" max="8" value="4">
            <button class="setup-btn" onclick="confirmSlider()">✓ Confirm</button>
          </div>
        </div>
        <canvas id="board" width="480" height="480"></canvas>
      </div>

      <!-- Web OK banner — shows when engine has moved and web user must acknowledge -->
      <div class="ok-banner" id="ok-banner">
        <span class="ok-banner-text" id="ok-banner-text">Engine moved — click OK to continue</span>
        <button class="ok-banner-btn" onclick="sendWebOk()">OK ✓</button>
      </div>

      <div class="board-footer">
        <button class="ctrl-btn" id="undo-btn" onclick="socket.emit('undo')">↩ Undo</button>
        <button class="ctrl-btn" id="hint-btn" onclick="socket.emit('hint')">💡 Hint</button>
        <button class="ctrl-btn primary full" onclick="socket.emit('new_game')">⟳ New Game</button>
      </div>
    </div>

    <div class="diag-row">
      <div class="card">
        <div class="card-title">OLED Screen Mirror</div>
        <div class="oled-wrap" id="oled-screen">
          <div class="oled-row" id="ol0"></div>
          <div class="oled-row" id="ol1"></div>
          <div class="oled-row" id="ol2"></div>
          <div class="oled-row" id="ol3"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Live Board LEDs</div>
        <div class="led-grid" id="led-grid"></div>
      </div>
    </div>
  </div>

  <div class="sidebar">

    <div class="card">
      <div class="card-title">Status</div>
      <div class="status-text" id="status-text">Connecting...</div>
      <div class="eval-wrap">
        <div class="eval-bar-bg"><div class="eval-bar" id="eval-bar" style="width:50%"></div></div>
        <div class="eval-label" id="eval-label">0</div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Controls</div>
      <div class="ctrl-grid">
        <button class="ctrl-btn" id="usb-btn" onclick="socket.emit('save_usb')" disabled>💾 Save</button>
        <a href="/api/pgn/download" style="display:contents"><button class="ctrl-btn">📥 PGN</button></a>
        <div class="toggle-row full" style="grid-column:1/-1">
          <span class="toggle-label" id="voice-label">Voice off</span>
          <label class="sw"><input type="checkbox" id="voice-toggle" onchange="socket.emit('toggle_voice')"><span class="sw-track"></span></label>
        </div>
      </div>
      <div class="usb-status no" id="usb-status">No USB drive detected</div>
      <!-- USB Game Loader -->
      <div id="usb-games-section" style="display:none;margin-top:8px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted)">Saved Games</div>
          <label style="font-size:10px;color:var(--gold);cursor:pointer;border:1px solid var(--gold-dim);padding:2px 8px;border-radius:20px;background:var(--gold-glow)" title="Import a PGN from your device">
            ⬆ Import PGN
            <input type="file" accept=".pgn,application/x-chess-pgn,text/plain" style="display:none" onchange="importPgnFile(this)">
          </label>
        </div>
        <div class="usb-games-list show" id="usb-games-list"></div>
      </div>
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
        <div class="theme-pill" data-t="fire" onclick="setTheme('fire')">Fire</div>
        <div class="theme-pill" data-t="neon" onclick="setTheme('neon')">Neon</div>
        <div class="theme-pill" data-t="custom" onclick="document.getElementById('ov-theme').classList.add('show')">Custom…</div>
      </div>
    </div>

    <div class="disco-hint" onclick="socket.emit('disco')">✨ tap for a surprise</div>
  </div>
</div>

<!-- ── Theme Designer ── -->
<div class="overlay" id="ov-theme">
  <div class="modal">
    <button class="modal-close" onclick="closeOv('ov-theme')">✕</button>
    <h2>🎨 Custom LED Theme</h2>
    <div class="theme-note">💡 Dark squares are auto-calculated at 50% brightness of your base colour.</div>
    <div class="picker-grid">
      <div class="picker-card" onclick="document.getElementById('c-light').click()">
        <input type="color" id="c-light" value="#f0d9b5" oninput="updatePickerFeedback('light',this.value)" onclick="event.stopPropagation()">
        <div class="picker-info"><span class="picker-label">Base Square</span><span class="picker-sub">Light squares</span></div>
      </div>
      <div class="picker-card" onclick="document.getElementById('c-move').click()">
        <input type="color" id="c-move" value="#e8e800" oninput="updatePickerFeedback('move',this.value)" onclick="event.stopPropagation()">
        <div class="picker-info"><span class="picker-label">Last Move</span><span class="picker-sub">Trail colour</span></div>
      </div>
      <div class="picker-card" onclick="document.getElementById('c-hint').click()">
        <input type="color" id="c-hint" value="#5588dd" oninput="updatePickerFeedback('hint',this.value)" onclick="event.stopPropagation()">
        <div class="picker-info"><span class="picker-label">Hint</span><span class="picker-sub">Engine suggestion</span></div>
      </div>
      <div class="picker-card" onclick="document.getElementById('c-legal').click()">
        <input type="color" id="c-legal" value="#40a020" oninput="updatePickerFeedback('legal',this.value)" onclick="event.stopPropagation()">
        <div class="picker-info"><span class="picker-label">Legal Targets</span><span class="picker-sub">Valid squares</span></div>
      </div>
    </div>
    <button class="ctrl-btn primary full" style="margin-top:16px" onclick="applyCustomTheme()">Apply Theme</button>
  </div>
</div>

<!-- ── PIN ── -->
<div class="overlay" id="ov-pin">
  <div class="pin-wrap">
    <div class="pin-title">🔒 Dev Access</div>
    <div class="pin-dots"><div class="pd" id="pd0"></div><div class="pd" id="pd1"></div><div class="pd" id="pd2"></div><div class="pd" id="pd3"></div></div>
    <div class="pin-pad">
      <button class="pin-key" onclick="pk('1')">1</button><button class="pin-key" onclick="pk('2')">2</button><button class="pin-key" onclick="pk('3')">3</button>
      <button class="pin-key" onclick="pk('4')">4</button><button class="pin-key" onclick="pk('5')">5</button><button class="pin-key" onclick="pk('6')">6</button>
      <button class="pin-key" onclick="pk('7')">7</button><button class="pin-key" onclick="pk('8')">8</button><button class="pin-key" onclick="pk('9')">9</button>
      <button class="pin-key" onclick="closeOv('ov-pin')" style="font-size:12px">✕</button>
      <button class="pin-key" onclick="pk('0')">0</button>
      <button class="pin-key" onclick="pdel()">⌫</button>
    </div>
    <div class="pin-err" id="pin-err"></div>
  </div>
</div>

<!-- ── Dev Panel ── -->
<div class="overlay" id="ov-dev">
  <div class="modal dev-modal">
    <button class="modal-close" onclick="closeOv('ov-dev')">✕</button>
    <h2>⚙️ Developer Settings</h2>

    <div class="dev-tabs">
      <div class="dev-tab active" onclick="switchDevTab('engine')">🤖 Engine</div>
      <div class="dev-tab" onclick="switchDevTab('hardware')">💡 Hardware</div>
      <div class="dev-tab" onclick="switchDevTab('gameplay')">🎮 Gameplay</div>
      <div class="dev-tab" onclick="switchDevTab('system')">🖥 System</div>
    </div>

    <div class="dev-tab-panel active" id="dtab-engine">
      <div class="dev-section">
        <div class="dev-section-title">Stockfish Difficulty</div>
        <div class="dev-row"><label>Default level (1–20)</label><input class="dev-input" type="number" id="d-difficulty_default" min="1" max="20"></div>
        <div class="dev-row"><label>Min level</label><input class="dev-input" type="number" id="d-difficulty_min" min="1" max="20"></div>
        <div class="dev-row"><label>Max level</label><input class="dev-input" type="number" id="d-difficulty_max" min="1" max="20"></div>
      </div>
      <div class="dev-section">
        <div class="dev-section-title">Move Time Limits</div>
        <div class="dev-row"><label>Default move time (ms)</label><input class="dev-input" type="number" id="d-movetime_default_ms" min="500" step="500"></div>
        <div class="dev-row"><label>Min move time (ms)</label><input class="dev-input" type="number" id="d-movetime_min_ms" min="500" step="500"></div>
        <div class="dev-row"><label>Max move time (ms)</label><input class="dev-input" type="number" id="d-movetime_max_ms" min="500" step="1000"></div>
      </div>
      <div class="dev-section">
        <div class="dev-section-title">Engine Path</div>
        <div class="dev-row"><label>Stockfish binary path</label><input class="dev-input wide" type="text" id="d-stockfish_path"></div>
      </div>
    </div>

    <div class="dev-tab-panel" id="dtab-hardware">
      <div class="dev-section">
        <div class="dev-section-title">LED Brightness <span class="warn-badge">⚠ power limit 76</span></div>
        <div class="dev-slider-row">
          <label>Brightness (0–255)</label>
          <input type="range" class="dev-slider" id="brightness-slider" min="0" max="120" value="76" oninput="onBrightnessSlide(this.value)">
          <span class="dev-slider-val" id="brightness-val">76</span>
        </div>
        <div style="font-size:10px;color:var(--orange);margin-bottom:8px">⚠ Values above 100 may draw excess current. Keep ≤76 for USB power.</div>
        <input type="hidden" id="d-chess_led_brightness" value="76">
      </div>
      <div class="dev-section">
        <div class="dev-section-title">Timing</div>
        <div class="dev-row"><label>Startup LED delay (s)</label><input class="dev-input" type="number" id="d-startup_led_delay_s" step="0.005"></div>
        <div class="dev-row"><label>Button debounce (s)</label><input class="dev-input" type="number" id="d-button_debounce_s" step="0.01"></div>
      </div>
    </div>

    <div class="dev-tab-panel" id="dtab-gameplay">
      <div class="dev-section">
        <div class="dev-section-title">Hints & Undo</div>
        <div class="dev-row"><label>Hint dismiss time (s)</label><input class="dev-input" type="number" id="d-hint_dismiss_s" step="0.5"></div>
        <div class="dev-row"><label>Max undo half-moves</label><input class="dev-input" type="number" id="d-undo_max_half_moves" min="1" max="50"></div>
      </div>
      <div class="dev-section">
        <div class="dev-section-title">Voice / TTS</div>
        <div class="dev-row"><label>Speech rate (wpm)</label><input class="dev-input" type="number" id="d-tts_rate" min="80" max="300" step="5"></div>
        <div class="dev-row"><label>Volume (0.0–1.0)</label><input class="dev-input" type="number" id="d-tts_volume" step="0.1" min="0" max="1"></div>
      </div>
    </div>

    <div class="dev-tab-panel" id="dtab-system">
      <div class="dev-section">
        <div class="dev-section-title">Server</div>
        <div class="dev-row"><label>Web port</label><input class="dev-input" type="number" id="d-web_port"></div>
        <div class="dev-row"><label>PGN subdirectory</label><input class="dev-input wide" type="text" id="d-pgn_subdir"></div>
        <div class="dev-row"><label>Dev PIN</label><input class="dev-input" type="text" id="d-dev_pin" maxlength="8"></div>
      </div>
      <div class="dev-section">
        <div class="dev-section-title">System Stats</div>
        <div id="sys-stats" style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text-dim);line-height:1.8">Loading...</div>
        <button class="ctrl-btn full" style="margin-top:8px" onclick="fetchSysStats()">↻ Refresh</button>
      </div>
    </div>

    <div style="display:flex;gap:7px;margin-top:14px">
      <button class="ctrl-btn primary" style="flex:1" onclick="saveDevSettings()">💾 Save Settings</button>
      <button class="ctrl-btn" onclick="closeOv('ov-dev')">Close</button>
    </div>
    <div class="dev-save-msg" id="dev-save-msg"></div>
  </div>
</div>

<!-- ── Game Viewer ── -->
<div class="overlay" id="ov-viewer">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);width:100%;max-width:820px;max-height:92vh;overflow:hidden;display:flex;flex-direction:column">

    <!-- Header -->
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border);flex-shrink:0">
      <div>
        <div style="font-size:15px;font-weight:700;color:var(--gold)" id="viewer-title">Game Viewer</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:2px" id="viewer-meta"></div>
      </div>
      <button class="modal-close" style="position:static;margin-left:14px" onclick="closeViewer()">✕</button>
    </div>

    <!-- Loading state -->
    <div id="viewer-loading" style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:48px;gap:14px;flex:1">
      <div style="font-size:32px">♟</div>
      <div style="font-size:14px;color:var(--text-dim)">Analysing game with Stockfish...</div>
      <div style="width:240px;display:flex;flex-direction:column;gap:5px">
        <div style="height:4px;border-radius:2px;background:var(--surface3);overflow:hidden">
          <div id="viewer-loading-bar" style="height:100%;width:0%;background:var(--gold);border-radius:2px;transition:width .2s"></div>
        </div>
        <div style="font-size:10px;color:var(--text-muted);text-align:center" id="viewer-loading-pct">0%</div>
      </div>
      <div style="font-size:11px;color:var(--text-muted)">Computing evaluation for every position</div>
    </div>

    <!-- Main content (hidden until loaded) -->
    <div id="viewer-content" style="display:none;grid-template-columns:380px 1fr;flex:1;overflow:hidden">

      <!-- Left: board + eval graph -->
      <div style="display:flex;flex-direction:column;background:var(--bg);padding:12px;gap:8px;border-right:1px solid var(--border)">
        <canvas id="viewer-board" width="380" height="380" style="border-radius:var(--r-md);width:100%"></canvas>

        <!-- Eval bar -->
        <div style="display:flex;align-items:center;gap:8px">
          <span style="font-size:10px;color:var(--text-dim);min-width:50px" id="viewer-turn-label">White</span>
          <div style="flex:1;height:5px;border-radius:3px;background:var(--surface3);overflow:hidden">
            <div id="viewer-eval-bar" style="height:100%;border-radius:3px;background:linear-gradient(90deg,var(--gold-dim),var(--gold));transition:width .35s;width:50%"></div>
          </div>
          <span style="font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--gold);min-width:40px;text-align:right" id="viewer-eval-text">0</span>
        </div>

        <!-- Eval graph -->
        <div style="background:var(--surface2);border:1px solid var(--border);border-radius:var(--r-md);overflow:hidden">
          <div style="font-size:9px;color:var(--text-muted);padding:4px 8px 2px;text-transform:uppercase;letter-spacing:.6px">Evaluation Graph</div>
          <canvas id="viewer-eval-graph" width="356" height="60" style="width:100%;display:block"></canvas>
        </div>
      </div>

      <!-- Right: move list + controls -->
      <div style="display:flex;flex-direction:column;overflow:hidden">

        <!-- Move counter + label -->
        <div style="padding:10px 14px;border-bottom:1px solid var(--border);flex-shrink:0;display:flex;align-items:center;justify-content:space-between">
          <div>
            <div style="font-size:10px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.6px">Position</div>
            <div style="font-size:18px;font-weight:700;color:var(--gold);line-height:1.2" id="viewer-move-num">0 / 0</div>
          </div>
          <div style="font-size:12px;color:var(--text-dim);text-align:right" id="viewer-move-label"></div>
        </div>

        <!-- Move list -->
        <div style="flex:1;overflow-y:auto;padding:6px 8px" id="viewer-move-list"></div>

        <!-- Controls -->
        <div style="padding:10px 12px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:6px;flex-shrink:0">

          <!-- Scrubber -->
          <input type="range" id="viewer-scrubber" min="0" value="0"
            style="width:100%;accent-color:var(--gold);height:4px" oninput="viewerGoto(+this.value)">

          <!-- Nav row -->
          <div style="display:flex;gap:4px;align-items:center">
            <button class="vbtn" onclick="viewerGoto(0)">⏮</button>
            <button class="vbtn" onclick="viewerStep(-5)">«</button>
            <button class="vbtn" onclick="viewerStep(-1)">◀</button>
            <button class="vbtn" id="viewer-play-btn" onclick="viewerTogglePlay()"
              style="flex:1;background:var(--gold);color:#111;font-weight:700;font-size:12px">▶ Play</button>
            <button class="vbtn" onclick="viewerStep(1)">▶</button>
            <button class="vbtn" onclick="viewerStep(5)">»</button>
            <button class="vbtn" onclick="viewerGoto(viewerMoves.length)">⏭</button>
          </div>

          <!-- Speed -->
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:10px;color:var(--text-muted);min-width:36px">Speed</span>
            <input type="range" id="viewer-speed" min="200" max="3000" value="800" step="100"
              style="flex:1;accent-color:var(--gold)">
            <span style="font-size:10px;font-family:'JetBrains Mono',monospace;color:var(--text-dim);min-width:28px"
              id="viewer-speed-label">0.8s</span>
          </div>

          <!-- Board sync + replay buttons -->
          <div style="display:flex;gap:6px">
            <button class="ctrl-btn" id="viewer-board-mode-btn" onclick="viewerToggleBoardMode()"
              style="flex:1;font-size:11px">⬆ Sync to Board</button>
            <button class="ctrl-btn primary" onclick="viewerLoadToBoard()"
              style="flex:1;font-size:11px">▶ Full Replay</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ── About ── -->
<div class="overlay" id="ov-about" onclick="closeOv('ov-about')">
  <div class="modal" style="text-align:center;max-width:340px" onclick="event.stopPropagation()">
    <button class="modal-close" onclick="closeOv('ov-about')">✕</button>
    <div style="font-size:40px;margin-bottom:8px">♟</div>
    <div style="font-size:18px;font-weight:700;color:var(--gold);margin-bottom:2px">Smart Chess Board</div>
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:14px">v3.1 · Jetson Orin Nano</div>
    <div class="about-chips">
      <span class="chip">Python 3</span><span class="chip">Flask + SocketIO</span><span class="chip">python-chess</span>
      <span class="chip">Stockfish</span><span class="chip">Jetson.GPIO</span><span class="chip">WS2812b</span><span class="chip">SSD1306</span>
    </div>
    <div style="font-size:13px;color:var(--text-dim)">Made with <span class="heart">❤️</span> by Richard P</div>
    <div style="margin-top:14px"><a href="https://github.com/RichardP111/jetson_chess" target="_blank" style="font-size:11px;color:var(--gold)">⎆ github.com/RichardP111/jetson_chess</a></div>
    <div style="font-size:10px;color:var(--text-muted);opacity:.4;margin-top:12px;cursor:pointer" onclick="socket.emit('disco')">psst...</div>
  </div>
</div>

<script>
const socket = io();
let currentFen = null, selectedSq = null, legalMoveSqs = [], lastMove = null, hintMove = null;
let gameActive = false, setupPhase = 'idle', webPlayer = null, discoActive = false;
let currentSliderPhase = 'difficulty', devUnlocked = false, pinBuf = '';
let waitingForOk = false;

const THEMES = {
  classic:{ light:'#f0d9b5', dark:'#b58863', sel:'#88ddff', move:'#e8e800', hint:'#4488ee', legal:'#3da020' },
  ocean:  { light:'#c9e8f0', dark:'#3a7fb5', sel:'#aaffdd', move:'#ffe040', hint:'#ff8040', legal:'#20c060' },
  forest: { light:'#e8f0c0', dark:'#5a8040', sel:'#ffd0a0', move:'#ffff60', hint:'#60a0ff', legal:'#30d060' },
  night:  { light:'#3a3f50', dark:'#1e2230', sel:'#8080ff', move:'#ffcc00', hint:'#ff6060', legal:'#40cc40' },
  fire:   { light:'#f08030', dark:'#6a1a00', sel:'#ffff00', move:'#ffff00', hint:'#ff4000', legal:'#ff8000' },
  neon:   { light:'#c000ff', dark:'#200030', sel:'#00ffaa', move:'#00ff80', hint:'#ff00c0', legal:'#00ffff' },
};
let customTheme = null, activeTheme = {...THEMES.classic};

socket.on('connect', () => {
  const p = document.getElementById('conn-pill');
  p.classList.add('on');
  document.getElementById('conn-txt').textContent = 'Connected';
});
socket.on('disconnect', () => {
  const p = document.getElementById('conn-pill');
  p.classList.remove('on');
  document.getElementById('conn-txt').textContent = 'Offline';
});

socket.on('state_update', d => {
  currentFen = d.fen;
  lastMove    = d.last_move || null;
  hintMove    = d.hint_move || null;
  gameActive  = d.game_active;
  setupPhase  = d.setup_phase || 'idle';
  webPlayer   = d.web_player || null;
  discoActive = d.disco_active || false;

  // Turn badge
  if (d.thinking) {
    document.getElementById('turn-badge').textContent = '⏳ Thinking...';
  } else if (!d.game_active) {
    document.getElementById('turn-badge').textContent = 'Waiting...';
  } else if (d.setup_phase && d.setup_phase !== 'idle') {
    document.getElementById('turn-badge').textContent = '⚙ Setup...';
  } else {
    document.getElementById('turn-badge').textContent = (d.whose_turn || '?') + "'s turn";
  }
  document.getElementById('mode-label').textContent = d.game_mode || '—';
  document.getElementById('status-text').textContent = d.status || '';

  // Eval bar
  const score = d.eval_score || 0;
  const pct = Math.max(5, Math.min(95, 50 + score / 20));
  document.getElementById('eval-bar').style.width = pct + '%';
  document.getElementById('eval-label').textContent = (score > 0 ? '+' : '') + score;

  // USB
  const usbAvail = !!d.usb_available;
  document.getElementById('usb-btn').disabled = !usbAvail;
  const usbEl = document.getElementById('usb-status');
  if (usbAvail) {
    usbEl.className = 'usb-status ok';
    usbEl.textContent = '✓ USB drive ready';
    // Show games list if present
    if (d.usb_games && d.usb_games.length > 0) {
      renderUsbGames(d.usb_games);
    }
  } else {
    usbEl.className = 'usb-status no';
    usbEl.textContent = 'No USB drive detected';
    document.getElementById('usb-games-section').style.display = 'none';
  }

  // Voice
  document.getElementById('voice-toggle').checked = !!d.voice_enabled;
  document.getElementById('voice-label').textContent = d.voice_enabled ? '🔊 Voice on' : '🔇 Voice off';

  // Ctrl visibility
  const playing = d.game_active && (d.setup_phase === 'idle' || !d.setup_phase);
  document.getElementById('undo-btn').style.display = playing ? 'flex' : 'none';
  document.getElementById('hint-btn').style.display = playing ? 'flex' : 'none';

  // OK banner — show when status contains "OK to continue" or similar
  // OK banner is handled by dedicated events (ok_banner_show/hide) below

  syncThemes(d.theme, d.custom_theme);
  renderHistory(d.move_history || []);
  handleSetupPhase(d);
  drawBoard();

  if (d.oled_lines) updateOled(d.oled_lines);
  document.getElementById('board-container').classList.toggle('disco-active', discoActive);
});

socket.on('legal_moves', d => {
  legalMoveSqs = d.moves.map(m => m.slice(2,4));
  drawBoard();
});
socket.on('move_rejected', d => {
  document.getElementById('status-text').textContent = '❌ ' + d.reason;
  selectedSq = null; legalMoveSqs = []; drawBoard();
});

// ── Dedicated OK banner events — arrive independently of state_update ──
socket.on('ok_banner_show', d => {
  const banner = document.getElementById('ok-banner');
  document.getElementById('ok-banner-text').textContent =
    '♟ ' + (d.move || 'Move') + ' played — acknowledge to continue';
  banner.classList.add('show');
});
socket.on('ok_banner_hide', () => {
  document.getElementById('ok-banner').classList.remove('show');
});

socket.on('oled_update', d => { if(d.lines) updateOled(d.lines); });

// USB games list from server
socket.on('usb_games_list', d => {
  if (d.available && d.games && d.games.length) renderUsbGames(d.games);
});

function renderUsbGames(games) {
  const section = document.getElementById('usb-games-section');
  const list = document.getElementById('usb-games-list');
  section.style.display = 'block';
  list.innerHTML = '';
  if (games.length === 0) {
    list.innerHTML = '<div style="font-size:11px;color:var(--text-muted);padding:6px 2px">No saved games on USB</div>';
    return;
  }
  games.forEach(name => {
    const row = document.createElement('div');
    row.className = 'usb-game-row';
    row.id = 'usbrow-' + name;

    const label = document.createElement('div');
    label.className = 'usb-game-name';
    label.textContent = name;
    label.title = name;

    const btnWrap = document.createElement('div');
    btnWrap.style.cssText = 'display:flex;gap:3px;flex-shrink:0';

    // View button
    const viewBtn = document.createElement('button');
    viewBtn.className = 'usb-game-btn';
    viewBtn.textContent = '👁';
    viewBtn.title = 'View game';
    viewBtn.style.cssText = 'background:rgba(79,184,247,.1);border-color:rgba(79,184,247,.4);color:var(--blue)';
    viewBtn.onclick = () => openViewer(name);

    // Load button
    const loadBtn = document.createElement('button');
    loadBtn.className = 'usb-game-btn';
    loadBtn.textContent = '▶';
    loadBtn.title = 'Replay on board';
    loadBtn.onclick = () => {
      if (confirm('Replay "' + name + '" on the physical board?')) {
        socket.emit('trigger_load_game', {filename: name});
        loadBtn.textContent = '⏳'; loadBtn.disabled = true;
        setTimeout(() => { loadBtn.textContent = '▶'; loadBtn.disabled = false; }, 3000);
      }
    };

    // Delete button
    const delBtn = document.createElement('button');
    delBtn.className = 'usb-game-btn';
    delBtn.textContent = '🗑';
    delBtn.title = 'Delete from USB';
    delBtn.style.cssText = 'background:rgba(240,80,80,.08);border-color:rgba(240,80,80,.3);color:var(--red)';
    delBtn.onclick = () => {
      if (confirm('Delete "' + name + '" from USB?\n\nThis cannot be undone.')) {
        delBtn.textContent = '⏳'; delBtn.disabled = true;
        socket.emit('delete_usb_game', {filename: name});
      }
    };

    btnWrap.appendChild(viewBtn);
    btnWrap.appendChild(loadBtn);
    btnWrap.appendChild(delBtn);
    row.appendChild(label);
    row.appendChild(btnWrap);
    list.appendChild(row);
  });
}

function importPgnFile(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    const pgn = e.target.result;
    const name = file.name.replace(/\.pgn$/i, '');
    const fd = new FormData();
    fd.append('file', file);
    fetch('/api/pgn/upload?name=' + encodeURIComponent(name), {method:'POST', body:fd})
      .then(r => r.json())
      .then(d => {
        if (d.ok) {
          document.getElementById('usb-status').textContent = '✓ Imported: ' + d.filename;
          socket.emit('request_usb_games');
        } else {
          alert('Import failed: ' + (d.error || 'unknown error'));
        }
      })
      .catch(() => alert('Upload failed — check USB is connected'));
  };
  reader.readAsText(file);
  input.value = '';  // reset so same file can be re-selected
}

socket.on('delete_usb_game_result', d => {
  if (d.ok) {
    // Animate row out then refresh list
    const row = document.getElementById('usbrow-' + d.filename);
    if (row) { row.style.opacity = '0'; row.style.transition = 'opacity .2s'; }
    setTimeout(() => renderUsbGames(d.games || []), 250);
  } else {
    alert('Delete failed: ' + (d.error || 'unknown error'));
  }
});

// ════════════════════════════════════════════════════════════
// GAME VIEWER ENGINE
// ════════════════════════════════════════════════════════════
let viewerMoves   = [];
let viewerPos     = 0;
let viewerBoards  = [];
let viewerEvals   = [];
let viewerPlaying = false;
let viewerTimer   = null;
let viewerFile    = '';
let viewerBoardMode = false;  // true = controlling physical board

const VIEWER_SZ = 47;
const vcanvas = document.getElementById('viewer-board');
const vctx    = vcanvas.getContext('2d');

function parseFenV(fen) {
  const pieces={};
  const rows=fen.split(' ')[0].split('/');
  for(let r=0;r<8;r++){let f=0;for(const ch of rows[r]){
    if('12345678'.includes(ch)){f+=+ch;}
    else{pieces[String.fromCharCode(97+f)+(8-r)]=(ch===ch.toUpperCase()?'w':'b')+ch.toUpperCase();f++;}
  }}
  return pieces;
}

function drawViewerBoard(fen, lastMoveUci) {
  if (!fen) return;
  const pieces = parseFenV(fen);
  const t = activeTheme;
  vctx.clearRect(0,0,380,380);
  for(let r=0;r<8;r++) for(let f=0;f<8;f++) {
    const sq = String.fromCharCode(97+f)+(8-r);
    const isLight = (f+r)%2===0;
    let col = isLight ? t.light : t.dark;
    if(lastMoveUci && (lastMoveUci.slice(0,2)===sq || lastMoveUci.slice(2,4)===sq)) col = t.move;
    vctx.fillStyle=col; vctx.fillRect(f*VIEWER_SZ,r*VIEWER_SZ,VIEWER_SZ,VIEWER_SZ);
    if(pieces[sq]){
      vctx.font=`${VIEWER_SZ*.7}px serif`;
      vctx.textAlign='center';vctx.textBaseline='middle';
      const isDark=pieces[sq][0]==='b';
      vctx.fillStyle=isDark?'#111':'#fff';
      vctx.strokeStyle=isDark?'#fff':'#222';
      vctx.lineWidth=2;
      vctx.strokeText(PIECES[pieces[sq]],f*VIEWER_SZ+VIEWER_SZ/2,r*VIEWER_SZ+VIEWER_SZ/2);
      vctx.fillText(PIECES[pieces[sq]],  f*VIEWER_SZ+VIEWER_SZ/2,r*VIEWER_SZ+VIEWER_SZ/2);
    }
    if(f===0){
      vctx.fillStyle=isLight?t.dark:t.light;vctx.font='9px Inter,sans-serif';
      vctx.textAlign='left';vctx.textBaseline='top';vctx.fillText(8-r,2,r*VIEWER_SZ+2);
    }
    if(r===7){
      vctx.fillStyle=isLight?t.dark:t.light;vctx.font='9px Inter,sans-serif';
      vctx.textAlign='right';vctx.textBaseline='bottom';
      vctx.fillText(String.fromCharCode(97+f),(f+1)*VIEWER_SZ-2,8*VIEWER_SZ-2);
    }
  }
}

function drawEvalGraph() {
  const canvas = document.getElementById('viewer-eval-graph');
  if (!canvas || viewerEvals.length === 0) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0,0,W,H);

  // Background
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--surface3').trim() || '#252a38';
  ctx.fillRect(0,0,W,H);

  // Zero line
  ctx.strokeStyle = 'rgba(255,255,255,0.12)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0,H/2); ctx.lineTo(W,H/2); ctx.stroke();

  if (viewerEvals.length < 2) return;

  // Clamp evals to ±1000cp for display
  const clamp = v => Math.max(-1000, Math.min(1000, v||0));
  const toY   = v => H/2 - (clamp(v)/1000)*(H/2-4);
  const toX   = i => (i/(viewerEvals.length-1))*W;

  // Fill area under curve (white advantage = above midline)
  ctx.beginPath();
  ctx.moveTo(toX(0), H/2);
  viewerEvals.forEach((v,i) => ctx.lineTo(toX(i), toY(v)));
  ctx.lineTo(toX(viewerEvals.length-1), H/2);
  ctx.closePath();
  ctx.fillStyle = 'rgba(201,168,76,0.2)';
  ctx.fill();

  // Eval line
  ctx.beginPath();
  viewerEvals.forEach((v,i) => {
    if(i===0) ctx.moveTo(toX(i),toY(v)); else ctx.lineTo(toX(i),toY(v));
  });
  ctx.strokeStyle = '#c9a84c';
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // Current position marker
  if (viewerPos > 0 && viewerPos < viewerEvals.length) {
    const mx = toX(viewerPos), my = toY(viewerEvals[viewerPos]);
    ctx.beginPath();
    ctx.arc(mx, my, 4, 0, Math.PI*2);
    ctx.fillStyle = '#fff';
    ctx.fill();
    ctx.strokeStyle = '#c9a84c';
    ctx.lineWidth = 2;
    ctx.stroke();
  }
}

function openViewer(filename) {
  viewerFile = filename;
  viewerMoves = []; viewerBoards = []; viewerEvals = [];
  viewerPos = 0; viewerBoardMode = false;
  stopPlay();
  document.getElementById('viewer-title').textContent = '⏳ Analysing game...';
  document.getElementById('viewer-meta').textContent = filename;
  document.getElementById('viewer-loading').style.display = 'flex';
  document.getElementById('viewer-content').style.display = 'none';
  const lb = document.getElementById('viewer-loading-bar');
  const lp = document.getElementById('viewer-loading-pct');
  if (lb) lb.style.width = '0%';
  if (lp) lp.textContent = '0%';
  document.getElementById('ov-viewer').classList.add('show');
  socket.emit('viewer_load_game', {filename});
}

socket.on('viewer_eval_progress', d => {
  const pct = d.total > 0 ? Math.round((d.done / d.total) * 100) : 0;
  const el = document.getElementById('viewer-loading-bar');
  const txt = document.getElementById('viewer-loading-pct');
  if (el)  el.style.width = pct + '%';
  if (txt) txt.textContent = pct + '%';
});

socket.on('viewer_game_data', d => {
  document.getElementById('viewer-loading').style.display = 'none';
  if (!d.ok) {
    document.getElementById('viewer-title').textContent = '❌ ' + (d.error || 'Error loading');
    return;
  }
  viewerMoves  = d.moves  || [];
  viewerBoards = d.fens   || [];
  viewerEvals  = d.evals  || [];
  const total  = viewerMoves.length;

  document.getElementById('viewer-title').textContent = d.white + ' vs ' + d.black;
  document.getElementById('viewer-meta').textContent  =
    d.date + ' · ' + d.event + ' · ' + Math.ceil(total/2) + ' moves · ' + d.result;
  document.getElementById('viewer-scrubber').max   = total;
  document.getElementById('viewer-scrubber').value = 0;
  document.getElementById('viewer-content').style.display = 'grid';

  viewerPos = 0;
  buildViewerMoveList();
  viewerUpdateDisplay();
  setTimeout(drawEvalGraph, 50);
});

function viewerUpdateDisplay() {
  const total = viewerMoves.length;
  const fen   = viewerBoards[viewerPos] || '';
  const lastMove = viewerPos > 0 ? viewerMoves[viewerPos-1] : null;

  drawViewerBoard(fen, lastMove);
  drawEvalGraph();

  const moveNum = Math.ceil(viewerPos/2);
  const isWhite = viewerPos%2===0;
  document.getElementById('viewer-move-num').textContent = viewerPos + ' / ' + total;
  document.getElementById('viewer-move-label').textContent =
    viewerPos===0 ? 'Start' : (moveNum + (isWhite?'. ♚':'... ♔') + ' ' + (lastMove||''));
  document.getElementById('viewer-scrubber').value = viewerPos;

  // Eval display
  const ev = viewerEvals[viewerPos] || 0;
  const evStr = Math.abs(ev) >= 9000 ? (ev>0?'+M':'-M') : ((ev>0?'+':'')+ev);
  document.getElementById('viewer-eval-text').textContent = evStr;
  const evPct = Math.max(5, Math.min(95, 50 + ev/20));
  document.getElementById('viewer-eval-bar').style.width = evPct + '%';

  const fen_turn = fen.split(' ')[1];
  document.getElementById('viewer-turn-label').textContent =
    viewerPos===total ? '🏁 Game over' : (fen_turn==='w' ? '⬜ White' : '⬛ Black');

  // Highlight current move in list
  document.querySelectorAll('.vml-move').forEach(el => {
    el.classList.toggle('vml-active', +el.dataset.idx === viewerPos);
    if (+el.dataset.idx === viewerPos) el.scrollIntoView({block:'nearest', behavior:'smooth'});
  });

  // If board mode is active, sync physical board
  if (viewerBoardMode) {
    socket.emit('viewer_board_goto', {pos: viewerPos});
  }
}

function buildViewerMoveList() {
  const list = document.getElementById('viewer-move-list');
  list.innerHTML = '';
  for(let i=0;i<viewerMoves.length;i+=2) {
    const row = document.createElement('div');
    row.style.cssText = 'display:grid;grid-template-columns:22px 1fr 1fr;gap:2px 3px;margin-bottom:1px;align-items:center';
    const num = document.createElement('span');
    num.style.cssText = 'font-size:10px;color:var(--text-muted);font-family:"JetBrains Mono",monospace;padding:2px 0';
    num.textContent = (i/2+1)+'.';
    const w = document.createElement('button');
    w.className='vml-move'; w.dataset.idx=i+1; w.textContent=viewerMoves[i];
    w.onclick=()=>viewerGoto(+w.dataset.idx);
    const b = document.createElement('button');
    b.className='vml-move'; b.dataset.idx=i+2;
    b.textContent=viewerMoves[i+1]||'';
    if(viewerMoves[i+1]) b.onclick=()=>viewerGoto(+b.dataset.idx);
    row.appendChild(num); row.appendChild(w); row.appendChild(b);
    list.appendChild(row);
  }
}

function viewerGoto(pos) {
  viewerPos = Math.max(0, Math.min(viewerMoves.length, pos));
  viewerUpdateDisplay();
}
function viewerStep(d) { viewerGoto(viewerPos+d); }
function stopPlay() {
  viewerPlaying = false; clearTimeout(viewerTimer);
  const btn = document.getElementById('viewer-play-btn');
  if (btn) { btn.textContent='▶ Play'; btn.style.background='var(--gold)'; btn.style.color='#111'; }
}
function viewerTogglePlay() {
  viewerPlaying = !viewerPlaying;
  const btn = document.getElementById('viewer-play-btn');
  if (viewerPlaying) {
    btn.textContent='⏸ Pause'; btn.style.background='var(--surface3)'; btn.style.color='var(--text)';
    viewerAutoStep();
  } else {
    stopPlay();
  }
}
function viewerAutoStep() {
  if (!viewerPlaying) return;
  if (viewerPos >= viewerMoves.length) { stopPlay(); return; }
  viewerStep(1);
  const speed = +document.getElementById('viewer-speed').value;
  document.getElementById('viewer-speed-label').textContent=(speed/1000).toFixed(1)+'s';
  viewerTimer = setTimeout(viewerAutoStep, speed);
}
document.getElementById('viewer-speed').addEventListener('input', function(){
  document.getElementById('viewer-speed-label').textContent=(this.value/1000).toFixed(1)+'s';
});

// Board mode — sync physical board to viewer position
function viewerToggleBoardMode() {
  viewerBoardMode = !viewerBoardMode;
  const btn = document.getElementById('viewer-board-mode-btn');
  if (viewerBoardMode) {
    btn.textContent = '🔴 Board Synced';
    btn.style.borderColor = 'var(--red)'; btn.style.color='var(--red)';
    socket.emit('viewer_board_load', {filename: viewerFile});
  } else {
    btn.textContent = '⬆ Sync to Board';
    btn.style.borderColor=''; btn.style.color='';
    socket.emit('viewer_board_stop', {});
  }
}
socket.on('viewer_board_status', d => {
  if (d.error) {
    document.getElementById('viewer-board-mode-btn').textContent = '❌ ' + d.error;
    viewerBoardMode = false;
  }
});

function viewerLoadToBoard() {
  if (!viewerFile) return;
  if (confirm('Replay "' + viewerFile + '" on the physical board?\nThis resets the current game.')) {
    socket.emit('trigger_load_game', {filename: viewerFile});
    closeViewer();
  }
}

function closeViewer() {
  stopPlay();
  if (viewerBoardMode) { socket.emit('viewer_board_stop',{}); viewerBoardMode=false; }
  document.getElementById('ov-viewer').classList.remove('show');
}

function sendWebOk() {
  socket.emit('web_ok');
  document.getElementById('ok-banner').classList.remove('show');
}

// ── Setup phase ──
function handleSetupPhase(d) {
  const panel = document.getElementById('setup-panel');
  const phase = d.setup_phase || 'idle';
  if (phase === 'idle' || phase === 'ready') { panel.classList.remove('show'); return; }
  panel.classList.add('show');
  const title = document.getElementById('setup-title');
  const sub   = document.getElementById('setup-sub');
  const btns  = document.getElementById('setup-btns');
  const slWrap = document.getElementById('setup-slider-wrap');
  const slider = document.getElementById('setup-slider');
  const slVal  = document.getElementById('setup-slider-val');
  btns.innerHTML = '';
  slWrap.style.display = 'none';

  if (phase === 'mode_select') {
    title.textContent = 'Select Game Mode';
    sub.textContent = 'How do you want to play?';
    [['stockfish','🤖 vs AI (Physical)'],['lichess','🌐 Lichess Online'],['local','👥 Local 2P (Physical)'],
     ['web_vs_ai','💻 Web vs AI'],['web_local','💻 Web 2 Player']].forEach(([mode,label]) => {
      const b = document.createElement('button');
      b.className = 'setup-btn'; b.textContent = label;
      b.onclick = () => socket.emit('web_mode_select', {mode});
      btns.appendChild(b);
    });
  } else if (phase === 'difficulty') {
    title.textContent = '🤖 AI Difficulty';
    sub.textContent = '1 = easiest  ·  8 = hardest';
    currentSliderPhase = 'difficulty';
    slWrap.style.display = 'flex';
    slider.min = 1; slider.max = 8; slider.value = 1; slVal.textContent = '1';
    socket.emit('slider_preview', {phase:'difficulty', value:1});
    slider.oninput = () => { slVal.textContent = slider.value; socket.emit('slider_preview', {phase:'difficulty', value:+slider.value}); };
  } else if (phase === 'time') {
    title.textContent = '⏱ Think Time';
    sub.textContent = 'How long should the engine think per move?';
    [[1,'⚡ 1s'],[2,'2s'],[3,'3s'],[4,'5s'],[5,'8s'],[6,'12s'],[7,'20s'],[8,'30s']].forEach(([idx,label]) => {
      const b = document.createElement('button');
      b.className = 'setup-btn'; b.textContent = label;
      b.onclick = () => { socket.emit('slider_preview', {phase:'time', value:idx}); setTimeout(()=>socket.emit('web_setup_answer', {value:idx}), 50); };
      btns.appendChild(b);
    });
  } else if (phase === 'colour') {
    title.textContent = '♟ Choose Colour';
    sub.textContent = '';
    [['white','♔ White'],['black','♚ Black']].forEach(([c,label]) => {
      const b = document.createElement('button'); b.className = 'setup-btn'; b.textContent = label;
      b.onclick = () => socket.emit('web_setup_answer', {value:c}); btns.appendChild(b);
    });
  } else if (phase === 'web_colour') {
    title.textContent = '💻 Your Colour';
    sub.textContent = 'Which side do you control from the browser?';
    [['White','♔ White'],['Black','♚ Black']].forEach(([c,label]) => {
      const b = document.createElement('button'); b.className = 'setup-btn'; b.textContent = label;
      b.onclick = () => socket.emit('web_setup_answer', {value:c}); btns.appendChild(b);
    });
  } else if (phase === 'online_opponent') {
    title.textContent = '🌐 Opponent Type';
    sub.textContent = 'Who do you want to play against on Lichess?';
    [
      ['human',   '👤 Real Human (rated)'],
      ['ai_easy', '🤖 Stockfish lvl 1 (easy)'],
      ['ai_hard', '🤖 Stockfish lvl 8 (hard)'],
    ].forEach(([val, label]) => {
      const b = document.createElement('button'); b.className = 'setup-btn'; b.textContent = label;
      b.onclick = () => socket.emit('web_setup_answer', {value: val}); btns.appendChild(b);
    });
  }
}
function confirmSlider() {
  socket.emit('web_setup_answer', {value: +document.getElementById('setup-slider').value});
}

// ── Board canvas ──
const canvas = document.getElementById('board');
const ctx    = canvas.getContext('2d');
const SZ = 60;
const PIECES = {wK:'♔',wQ:'♕',wR:'♖',wB:'♗',wN:'♘',wP:'♙',bK:'♚',bQ:'♛',bR:'♜',bB:'♝',bN:'♞',bP:'♟'};

function sqToXY(sq){const f=sq.charCodeAt(0)-97;const r=+sq[1]-1;return{x:f*SZ,y:(7-r)*SZ}}
function xyToSq(x,y){const f=Math.floor(x/SZ);const r=7-Math.floor(y/SZ);if(f<0||f>7||r<0||r>7)return null;return String.fromCharCode(97+f)+(r+1)}
function parseFen(fen){
  const pieces={};const rows=fen.split(' ')[0].split('/');
  for(let r=0;r<8;r++){let f=0;for(const ch of rows[r]){if('12345678'.includes(ch)){f+=+ch}else{pieces[String.fromCharCode(97+f)+(8-r)]=(ch===ch.toUpperCase()?'w':'b')+ch.toUpperCase();f++}}}
  return pieces;
}
function drawBoard() {
  if (!currentFen) return;
  const pieces = parseFen(currentFen);
  const t = activeTheme;
  ctx.clearRect(0,0,480,480);
  for(let r=0;r<8;r++) for(let f=0;f<8;f++) {
    const sq = String.fromCharCode(97+f)+(8-r);
    const isLight = (f+r)%2===0;
    let col = isLight ? t.light : t.dark;
    if(selectedSq===sq) col=t.sel;
    else if(lastMove&&(lastMove.slice(0,2)===sq||lastMove.slice(2,4)===sq)) col=t.move;
    else if(hintMove&&(hintMove.slice(0,2)===sq||hintMove.slice(2,4)===sq)) col=t.hint;
    else if(legalMoveSqs.includes(sq)) col=t.legal;
    ctx.fillStyle=col; ctx.fillRect(f*SZ,r*SZ,SZ,SZ);
    if(legalMoveSqs.includes(sq)&&col===t.legal){
      ctx.fillStyle='rgba(0,0,0,.25)';ctx.beginPath();ctx.arc(f*SZ+SZ/2,r*SZ+SZ/2,SZ*.15,0,Math.PI*2);ctx.fill();
    }
    if(pieces[sq]){
      ctx.font=`${SZ*.68}px serif`;ctx.textAlign='center';ctx.textBaseline='middle';
      const isDark=pieces[sq][0]==='b';
      ctx.fillStyle=isDark?'#111':'#fff';ctx.strokeStyle=isDark?'#fff':'#222';ctx.lineWidth=2.5;
      ctx.strokeText(PIECES[pieces[sq]],f*SZ+SZ/2,r*SZ+SZ/2);
      ctx.fillText(PIECES[pieces[sq]],f*SZ+SZ/2,r*SZ+SZ/2);
    }
    if(f===0){ctx.fillStyle=isLight?t.dark:t.light;ctx.font='10px Inter,sans-serif';ctx.textAlign='left';ctx.textBaseline='top';ctx.fillText(8-r,2,r*SZ+2);}
    if(r===7){ctx.fillStyle=isLight?t.dark:t.light;ctx.font='10px Inter,sans-serif';ctx.textAlign='right';ctx.textBaseline='bottom';ctx.fillText(String.fromCharCode(97+f),(f+1)*SZ-2,8*SZ-2);}
  }
}
canvas.addEventListener('click', e => {
  if(!gameActive||setupPhase!=='idle') return;
  const rect=canvas.getBoundingClientRect();
  const x=(e.clientX-rect.left)*(480/rect.width);
  const y=(e.clientY-rect.top)*(480/rect.height);
  const sq=xyToSq(x,y);
  if(!sq) return;
  if(selectedSq) {
    const uci=selectedSq+sq;
    selectedSq=null; legalMoveSqs=[];
    socket.emit('move_input',{uci});
    drawBoard();
  } else {
    selectedSq=sq; legalMoveSqs=[];
    socket.emit('get_legal_moves',{fen:currentFen,sq});
    drawBoard();
  }
});

// ── History ──
function renderHistory(history) {
  const el = document.getElementById('history-list');
  el.innerHTML = '';
  for(let i=0;i<history.length;i+=2){
    const num=document.createElement('span');num.className='mv-num';num.textContent=(i/2+1)+'.';
    const w=document.createElement('span');w.className='mv-w'+(i===history.length-1?' last':'');w.textContent=history[i];
    const b=document.createElement('span');b.className='mv-b'+(i+1===history.length-1?' last':'');b.textContent=history[i+1]||'';
    el.appendChild(num);el.appendChild(w);el.appendChild(b);
  }
  el.scrollTop=el.scrollHeight;
}

// ── Themes ──
function setTheme(t) { socket.emit('set_theme',{theme:t}); }
function syncThemes(t, customData) {
  if(t==='custom'&&customData) customTheme=customData;
  activeTheme = (t==='custom'&&customTheme) ? customTheme : (THEMES[t]||THEMES.classic);
  document.querySelectorAll('.theme-pill').forEach(p=>p.classList.toggle('active',p.dataset.t===t));
  drawBoard();
}
function updatePickerFeedback(k,hex){
  const c=document.getElementById('card-'+k);if(c){c.style.borderColor=hex;c.style.boxShadow=`0 0 8px ${hex}40`;}
}
function applyCustomTheme() {
  const lh=document.getElementById('c-light').value;
  const half=c=>{const n=parseInt(c,16);return Math.floor(n/2).toString(16).padStart(2,'0')};
  const ct={
    light:lh,
    dark:`#${half(lh.slice(1,3))}${half(lh.slice(3,5))}${half(lh.slice(5,7))}`,
    sel:'#88ddff',
    move:document.getElementById('c-move').value,
    hint:document.getElementById('c-hint').value,
    legal:document.getElementById('c-legal').value,
  };
  customTheme=ct;
  socket.emit('set_custom_theme',ct);
  closeOv('ov-theme');
}

// ── OLED mirror ──
function updateOled(lines) {
  for(let i=0;i<4;i++){
    const el=document.getElementById('ol'+i);if(!el)continue;
    const txt=lines[i]||'';
    el.textContent=txt;
    el.className='oled-row'+(i===0&&txt&&!txt.startsWith(' ')?' header':'');
  }
}

// ── PIN ──
function openDev(){if(devUnlocked){showDevPanel();return;}pinBuf='';updPinDots();document.getElementById('pin-err').textContent='';document.getElementById('ov-pin').classList.add('show');}
function pk(d){if(pinBuf.length>=4)return;pinBuf+=d;updPinDots();if(pinBuf.length===4)checkPin();}
function pdel(){pinBuf=pinBuf.slice(0,-1);updPinDots();document.getElementById('pin-err').textContent='';}
function updPinDots(){for(let i=0;i<4;i++)document.getElementById('pd'+i).className='pd'+(i<pinBuf.length?' filled':'');}
function checkPin(){socket._pendingPin=pinBuf;socket.emit('get_dev_settings');}

// ── Dev panel ──
socket.on('dev_settings', d => {
  if(socket._pendingPin!==undefined){
    const ok=socket._pendingPin===String(d.dev_pin||'1234');
    if(ok){devUnlocked=true;closeOv('ov-pin');showDevPanel();document.getElementById('dev-btn').classList.add('active-dev');}
    else{document.getElementById('pin-err').textContent='Incorrect PIN';pinBuf='';updPinDots();}
    socket._pendingPin=undefined;
  }
  // Populate all dev fields
  const map={
    'd-difficulty_default':d.difficulty_default,'d-difficulty_min':d.difficulty_min,
    'd-difficulty_max':d.difficulty_max,'d-movetime_default_ms':d.movetime_default_ms,
    'd-movetime_min_ms':d.movetime_min_ms,'d-movetime_max_ms':d.movetime_max_ms,
    'd-startup_led_delay_s':d.startup_led_delay_s,'d-button_debounce_s':d.button_debounce_s,
    'd-hint_dismiss_s':d.hint_dismiss_s,'d-undo_max_half_moves':d.undo_max_half_moves,
    'd-stockfish_path':d.stockfish_path,'d-tts_rate':d.tts_rate,'d-tts_volume':d.tts_volume,
    'd-web_port':d.web_port,'d-pgn_subdir':d.pgn_subdir,'d-dev_pin':d.dev_pin,
  };
  Object.entries(map).forEach(([id,val])=>{const el=document.getElementById(id);if(el)el.value=val;});
  // Set brightness slider
  const bval=d.chess_led_brightness||76;
  document.getElementById('brightness-slider').value=bval;
  document.getElementById('brightness-val').textContent=bval;
  document.getElementById('d-chess_led_brightness').value=bval;
});

socket.on('dev_settings_saved', d => {
  const msg=document.getElementById('dev-save-msg');
  msg.style.color=d.ok?'var(--green)':'var(--red)';
  msg.textContent=d.ok?'✓ Saved — '+d.changed.length+' setting(s) updated':'Error saving settings';
  setTimeout(()=>{msg.textContent='';},3000);
});

function showDevPanel(){socket.emit('get_dev_settings');document.getElementById('ov-dev').classList.add('show');fetchSysStats();}
function switchDevTab(name){
  document.querySelectorAll('.dev-tab').forEach((t,i)=>{
    const names=['engine','hardware','gameplay','system'];
    t.classList.toggle('active',names[i]===name);
    document.getElementById('dtab-'+names[i]).classList.toggle('active',names[i]===name);
  });
}
function onBrightnessSlide(val){
  document.getElementById('brightness-val').textContent=val;
  document.getElementById('d-chess_led_brightness').value=val;
  // Live preview on hardware — debounced 100ms
  clearTimeout(window._bDebounce);
  window._bDebounce=setTimeout(()=>socket.emit('set_brightness_live',{value:+val}),100);
}
function saveDevSettings(){
  const payload={
    difficulty_default:document.getElementById('d-difficulty_default').value,
    difficulty_min:document.getElementById('d-difficulty_min').value,
    difficulty_max:document.getElementById('d-difficulty_max').value,
    movetime_default_ms:document.getElementById('d-movetime_default_ms').value,
    movetime_min_ms:document.getElementById('d-movetime_min_ms').value,
    movetime_max_ms:document.getElementById('d-movetime_max_ms').value,
    chess_led_brightness:document.getElementById('d-chess_led_brightness').value,
    startup_led_delay_s:document.getElementById('d-startup_led_delay_s').value,
    button_debounce_s:document.getElementById('d-button_debounce_s').value,
    hint_dismiss_s:document.getElementById('d-hint_dismiss_s').value,
    undo_max_half_moves:document.getElementById('d-undo_max_half_moves').value,
    stockfish_path:document.getElementById('d-stockfish_path').value,
    tts_rate:document.getElementById('d-tts_rate').value,
    tts_volume:document.getElementById('d-tts_volume').value,
    web_port:document.getElementById('d-web_port').value,
    pgn_subdir:document.getElementById('d-pgn_subdir').value,
    dev_pin:document.getElementById('d-dev_pin').value,
  };
  socket.emit('apply_dev_settings',payload);
}

function fetchSysStats(){
  fetch('/api/system').then(r=>r.json()).then(d=>{
    document.getElementById('sys-stats').innerHTML=
      `CPU: ${d.cpu_pct}% &nbsp; Temp: ${d.cpu_temp_c}°C<br>RAM: ${d.ram_used_mb}MB / ${d.ram_total_mb}MB`;
  }).catch(()=>{document.getElementById('sys-stats').textContent='Stats unavailable';});
}

function showAbout(){document.getElementById('ov-about').classList.add('show');}

function switchProject(target) {
  const names = {squid: 'Squid Game', chess: 'Chess Board'};
  const icons = {squid: '🟢', chess: '♟'};
  const name = names[target] || target;
  if (!confirm('Switch to ' + name + '?\n\nThe chess board will shut down cleanly first.')) return;
  document.getElementById('switch-icon').textContent = icons[target] || '🔄';
  document.getElementById('switch-title').textContent = 'Switching to ' + name + '...';
  document.getElementById('switch-msg').textContent = 'Shutting down chess board safely';
  document.getElementById('switch-bar').style.width = '0%';
  document.getElementById('ov-switch').classList.add('show');
  // Animate progress bar
  let pct = 0;
  const iv = setInterval(() => {
    pct = Math.min(90, pct + 8);
    document.getElementById('switch-bar').style.width = pct + '%';
  }, 300);
  socket.emit('switch_project', {target});
  socket._switchInterval = iv;
}

socket.on('switch_project_status', d => {
  clearInterval(socket._switchInterval);
  document.getElementById('switch-bar').style.width = '100%';
  if (d.ok) {
    document.getElementById('switch-msg').textContent = d.message || 'Done — page will reload';
    setTimeout(() => { window.location.reload(); }, 2000);
  } else {
    document.getElementById('switch-title').textContent = '❌ Switch failed';
    document.getElementById('switch-msg').textContent = d.error || 'Check the service name in .env';
    document.getElementById('switch-bar').style.background = 'var(--red)';
    setTimeout(() => document.getElementById('ov-switch').classList.remove('show'), 4000);
  }
});
function closeOv(id){document.getElementById(id).classList.remove('show');}
function toggleTheme(){const h=document.documentElement;h.dataset.theme=h.dataset.theme==='dark'?'light':'dark';}

document.addEventListener('keydown',e=>{if(e.key==='Escape'){['ov-pin','ov-dev','ov-about','ov-theme','ov-viewer'].forEach(closeOv);closeViewer();}});
['ov-about','ov-theme'].forEach(id=>{document.getElementById(id).addEventListener('click',function(e){if(e.target===this)closeOv(id);});});
document.getElementById('ov-viewer').addEventListener('click',function(e){if(e.target===this)closeViewer();});

// ── LED Grid ──
(function(){
  const g=document.getElementById('led-grid');
  for(let i=0;i<64;i++){const c=document.createElement('div');c.className='led-cell';c.id='lc'+i;g.appendChild(c);}
  function updateGrid(px){
    for(let i=0;i<64;i++){
      const [r,gr,b]=(px[i]||[0,0,0]);
      const el=document.getElementById('lc'+i);if(!el)continue;
      if(r+gr+b<8){el.style.background='#0a0a0a';el.style.boxShadow='none';}
      else{const hex='#'+[r,gr,b].map(x=>x.toString(16).padStart(2,'0')).join('');el.style.background=hex;el.style.boxShadow=`0 0 5px 1px ${hex}88`;}
    }
  }
  socket.on('led_grid',d=>updateGrid(d.grid));
  socket.on('state_update',d=>{if(d.led_grid)updateGrid(d.led_grid);});
})();

// ── Splash ──
(function(){
  const s=document.createElement('div');
  s.innerHTML='<div style="font-size:48px;margin-bottom:16px">♟</div><div style="font-size:18px;font-weight:700;color:var(--gold);margin-bottom:4px">Smart Chess Board</div><div style="font-size:12px;color:var(--text-dim);margin-bottom:24px">Connecting to board...</div><div style="width:160px;height:3px;background:var(--surface3);border-radius:2px;overflow:hidden"><div id="splash-bar" style="height:100%;width:0;background:var(--gold);border-radius:2px;transition:width .25s"></div></div><div style="position:absolute;bottom:20px;font-size:10px;color:var(--text-muted)">Made with ❤ by Richard P</div>';
  Object.assign(s.style,{position:'fixed',inset:'0',background:'var(--bg)',display:'flex',flexDirection:'column',alignItems:'center',justifyContent:'center',zIndex:'9999',transition:'opacity .4s'});
  document.body.appendChild(s);
  const HOLD=__SPLASH_MS__;const start=Date.now();
  const iv=setInterval(()=>{const p=Math.min(100,(Date.now()-start)/HOLD*100);const b=document.getElementById('splash-bar');if(b)b.style.width=p+'%';if(p>=100)clearInterval(iv);},30);
  setTimeout(()=>{clearInterval(iv);const b=document.getElementById('splash-bar');if(b)b.style.width='100%';setTimeout(()=>{s.style.opacity='0';setTimeout(()=>s.remove(),400);},80);},HOLD);
})();

drawBoard();
</script>
<div style="text-align:center;padding:16px 0 24px;font-size:10px;color:var(--text-muted);opacity:.4">
  Made with <span style="color:#e04040;animation:hb .8s infinite;display:inline-block">❤</span> by Richard P &nbsp;·&nbsp;
  <a href="https://github.com/RichardP111/jetson_chess" target="_blank" style="color:inherit">GitHub</a> &nbsp;·&nbsp; v3.1
</div>
</body>
</html>
"""