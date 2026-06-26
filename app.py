#!/usr/bin/env python3
"""Folder Mover - Web UI for organizing downloads to NAS."""

import json
import os
import secrets
import shutil
import time
import logging
import threading
from functools import wraps
from flask import (Flask, render_template, jsonify, request,
                   Response, stream_with_context, session, redirect, url_for)

from config import load_config
from scanner import scan_downloads
from mover import execute_moves, get_progress, reset_progress
import history as hist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
config = load_config()
app.secret_key = config.get("secret_key") or secrets.token_hex(32)

# Global move state
_move_results: list = []
_move_inputs:  list = []
_move_lock = threading.Lock()


# ── Auth ──────────────────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            # API calls → 401 JSON, page calls → redirect
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Not authenticated"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if (username == config["auth"]["username"]
                and password == config["auth"]["password"]):
            session.permanent = False   # survives until browser closes
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Falscher Benutzername oder Passwort."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Pages & API ───────────────────────────────────────────────────────────────

@app.route("/")
@require_auth
def index():
    return render_template("index.html", config=config)


# Global scan state
_scan_progress: dict = {}
_scan_results:  list = []
_scan_lock = threading.Lock()


@app.route("/api/scan", methods=["POST"])
@require_auth
def api_scan():
    with _scan_lock:
        _scan_progress.clear()
        _scan_progress.update({"phase": "init", "current": "Starte…", "found": 0, "done": False, "error": None})
        _scan_results.clear()

    def run():
        try:
            def on_progress(state):
                with _scan_lock:
                    _scan_progress.update(state)

            results = scan_downloads(config, on_progress=on_progress)
            with _scan_lock:
                _scan_results.extend(results)
                _scan_progress.update({"phase": "done", "found": len(results), "done": True})
        except Exception as e:
            log.exception("Scan failed")
            with _scan_lock:
                _scan_progress.update({"phase": "error", "error": str(e), "done": True})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan started"})


@app.route("/api/scan-progress")
@require_auth
def api_scan_progress():
    def generate():
        while True:
            with _scan_lock:
                state = dict(_scan_progress)
            yield f"data: {json.dumps(state)}\n\n"
            if state.get("done"):
                with _scan_lock:
                    results = list(_scan_results)
                yield f"data: {json.dumps({'done': True, 'items': results})}\n\n"
                return
            time.sleep(0.15)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/move", methods=["POST"])
@require_auth
def api_move():
    data = request.get_json()
    moves = data.get("moves", [])
    if not moves:
        return jsonify({"ok": False, "error": "No moves provided"}), 400

    reset_progress()
    with _move_lock:
        _move_results.clear()
        _move_inputs.clear()
        _move_inputs.extend(moves)

    def run():
        results = execute_moves(moves, config)
        with _move_lock:
            _move_results.extend(results)
        hist.append(moves, results)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "message": "Move started"})


@app.route("/api/progress")
@require_auth
def api_progress():
    def generate():
        while True:
            p = get_progress()
            if p:
                yield f"data: {json.dumps(p)}\n\n"
                if p.get("finished"):
                    with _move_lock:
                        results = list(_move_results)
                    yield f"data: {json.dumps({'finished': True, 'results': results})}\n\n"
                    return
            else:
                yield f"data: {json.dumps({'active': False, 'finished': False})}\n\n"
            time.sleep(0.25)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/delete-nas", methods=["POST"])
@require_auth
def api_delete_nas():
    data = request.get_json()
    path = data.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "No path provided"}), 400

    # Safety: only allow deletion inside movies_dir or series_dir
    movies_dir = os.path.realpath(config["movies_dir"])
    series_dir = os.path.realpath(config["series_dir"])
    target = os.path.realpath(path)

    allowed = (
        target.startswith(movies_dir + os.sep) or
        target.startswith(series_dir + os.sep)
    )
    if not allowed:
        log.warning("Refused NAS delete outside media dirs: %s", target)
        return jsonify({"ok": False, "error": "Path outside media directories"}), 403

    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        elif os.path.isfile(target):
            os.unlink(target)
        else:
            return jsonify({"ok": False, "error": "Path not found"}), 404
        log.info("Deleted NAS path: %s", target)
        return jsonify({"ok": True})
    except Exception as e:
        log.exception("NAS delete failed: %s", target)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/delete", methods=["POST"])
@require_auth
def api_delete():
    data = request.get_json()
    source_top = data.get("source_top", "")
    if not source_top:
        return jsonify({"ok": False, "error": "No path provided"}), 400

    dl_dir = os.path.realpath(config["download_dir"])
    target = os.path.realpath(source_top)

    if not target.startswith(dl_dir + os.sep) and target != dl_dir:
        log.warning("Refused delete outside download_dir: %s", target)
        return jsonify({"ok": False, "error": "Path outside download directory"}), 403

    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        elif os.path.isfile(target):
            os.unlink(target)
        else:
            return jsonify({"ok": False, "error": "Path not found"}), 404
        log.info("Deleted: %s", target)
        return jsonify({"ok": True})
    except Exception as e:
        log.exception("Delete failed: %s", target)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/history")
@require_auth
def api_history():
    limit = min(int(request.args.get("limit", 200)), 500)
    return jsonify({"ok": True, "entries": hist.read(limit)})


if __name__ == "__main__":
    port = config.get("port", 8080)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
