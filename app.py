#!/usr/bin/env python3
"""Folder Mover - Web UI for organizing downloads to NAS."""

import json
import os
import shutil
import time
import logging
import threading
from functools import wraps
from flask import Flask, render_template, jsonify, request, Response, stream_with_context

from config import load_config
from scanner import scan_downloads
from mover import execute_moves, get_progress, reset_progress
import history as hist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
config = load_config()

# Global move results storage (single-user tool)
_move_results: list = []
_move_inputs:  list = []   # parallel to _move_results for history
_move_lock = threading.Lock()


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if (not auth
                or auth.username != config["auth"]["username"]
                or auth.password != config["auth"]["password"]):
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Folder Mover"'},
            )
        return f(*args, **kwargs)
    return decorated


@app.route("/")
@require_auth
def index():
    return render_template("index.html", config=config)


@app.route("/api/scan", methods=["POST"])
@require_auth
def api_scan():
    try:
        results = scan_downloads(config)
        return jsonify({"ok": True, "items": results})
    except Exception as e:
        log.exception("Scan failed")
        return jsonify({"ok": False, "error": str(e)}), 500


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

    t = threading.Thread(target=run, daemon=True)
    t.start()

    return jsonify({"ok": True, "message": "Move started"})


@app.route("/api/progress")
@require_auth
def api_progress():
    """Server-Sent Events stream for live move progress."""
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
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/delete", methods=["POST"])
@require_auth
def api_delete():
    data = request.get_json()
    source_top = data.get("source_top", "")
    if not source_top:
        return jsonify({"ok": False, "error": "No path provided"}), 400

    dl_dir = os.path.realpath(config["download_dir"])
    target = os.path.realpath(source_top)

    # Safety: only allow deletion inside download_dir
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
