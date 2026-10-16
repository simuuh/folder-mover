#!/usr/bin/env python3
"""Folder Mover - Web UI for organizing downloads to NAS."""

import argparse
import getpass
import hmac
import json
import logging
import os
import secrets
import shutil
import threading
import time
from functools import wraps

from flask import Flask, Response, jsonify, redirect, render_template, request, session, stream_with_context, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import history as hist
import i18n
from config import DEFAULT_PASSWORD, check_config, dir_overlaps, library_warnings, load_config, missing_mount_markers
from mover import execute_moves, get_progress, mark_failed, mark_pending
from scanner import scan_downloads

__version__ = "1.3.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _load_secret_key(cfg: dict) -> str:
    """
    Configured secret_key, else one persisted next to the history file (so
    sessions survive restarts), else a random one for this process only.
    """
    if cfg.get("secret_key"):
        return cfg["secret_key"]
    path = hist.HISTORY_FILE.parent / ".secret_key"
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        key = secrets.token_hex(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
        return key
    except OSError as e:
        log.warning("Could not persist secret key (%s) — sessions end on restart", e)
        return secrets.token_hex(32)


app = Flask(__name__)
config = load_config()
app.secret_key = _load_secret_key(config)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

def password_configured() -> bool:
    """A hash, or a plaintext password that is neither empty nor the shipped default."""
    auth = config["auth"]
    return bool(auth.get("password_hash")) or str(auth.get("password") or "") not in ("", DEFAULT_PASSWORD)


if not password_configured():
    log.error("No password set (or still the default %r) — login is disabled until auth.password_hash "
              "(python app.py --hash-password) or FOLDER_MOVER_PASSWORD_HASH is set.", DEFAULT_PASSWORD)
for _problem in check_config(config):
    log.warning("Config: %s", _problem)
if not os.access(hist.HISTORY_FILE.parent, os.W_OK):
    log.error("Data directory %s is not writable (uid %s) — history and session secret cannot be saved. "
              "Fix the folder owner (Docker: set PUID/PGID).", hist.HISTORY_FILE.parent,
              os.getuid() if hasattr(os, "getuid") else "?")

# Global move state (single-user tool: only one move at a time)
_move_results: list = []
_move_lock = threading.Lock()
_move_running = False


# ── i18n ──────────────────────────────────────────────────────────────────────

def current_language() -> str:
    return i18n.pick_language(request.cookies.get(i18n.COOKIE), config.get("language"),
                              request.accept_languages)


def _(key: str, **args) -> str:
    """Translate for the current request."""
    return i18n.translate(current_language(), key, **args)


@app.context_processor
def inject_i18n():
    lang = current_language()
    return {
        "lang": lang,
        "languages": i18n.LANGUAGES,
        "t": lambda key, **args: i18n.translate(lang, key, **args),
        "i18n_catalog": i18n.catalog(lang),
    }


def api_error(key: str, status: int, **args):
    """JSON error in the user's language; `error_key` stays stable for API clients."""
    return jsonify({"ok": False, "error": _(key, **args), "error_key": key}), status


# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
    )
    return resp


# ── Auth ──────────────────────────────────────────────────────────────────────

LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            # API calls → 401 JSON, page calls → redirect
            if request.path.startswith("/api/"):
                return api_error("api.not_authenticated", 401)
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def _check_credentials(username: str, password: str) -> bool:
    auth = config["auth"]
    user_ok = hmac.compare_digest(username.encode(), str(auth.get("username", "")).encode())
    if auth.get("password_hash"):
        pw_ok = check_password_hash(auth["password_hash"], password)
    else:
        pw_ok = hmac.compare_digest(password.encode(), str(auth.get("password", "")).encode())
    return user_ok and pw_ok


def _login_blocked(ip: str) -> bool:
    now = time.monotonic()
    with _login_lock:
        # Drop expired entries of all IPs so the dict cannot grow without bound
        for key in list(_login_failures):
            _login_failures[key] = [t for t in _login_failures[key] if now - t < LOGIN_LOCKOUT_SECONDS]
            if not _login_failures[key]:
                del _login_failures[key]
        return len(_login_failures.get(ip, [])) >= LOGIN_MAX_FAILURES


def _record_login_failure(ip: str):
    with _login_lock:
        _login_failures.setdefault(ip, []).append(time.monotonic())


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if not password_configured():
        # Never accept "changeme" or an empty password — explain how to set one
        return render_template("login.html", error=_("login.setup_required", default=DEFAULT_PASSWORD)), 403
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _login_blocked(ip):
            log.warning("Login blocked (too many failures) for %s", ip)
            error = _("login.locked")
            return render_template("login.html", error=error), 429
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if _check_credentials(username, password):
            session.clear()
            session.permanent = False   # survives until browser closes
            session["logged_in"] = True
            return redirect(url_for("index"))
        _record_login_failure(ip)
        log.warning("Failed login for user %r from %s", username, ip)
        error = _("login.wrong")
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    # POST only: a GET logout could be triggered by any foreign page/image
    session.clear()
    return redirect(url_for("login"))


def require_sane_dirs(f):
    """Refuse file operations while download/media directories overlap."""
    @wraps(f)
    def decorated(*args, **kwargs):
        overlaps = dir_overlaps(config)
        if overlaps:
            a, b = overlaps[0]
            log.error("Refused %s: %s and %s overlap", request.path, a, b)
            return api_error("api.dirs_overlap", 409, a=config[a], b=config[b])
        return f(*args, **kwargs)
    return decorated


# ── Path validation ───────────────────────────────────────────────────────────

def _is_within(path: str, root: str, allow_equal: bool = False) -> bool:
    """True if `path` (after resolving symlinks and ..) lies inside `root`."""
    target = os.path.realpath(path)
    base = os.path.realpath(root)
    if target == base:
        return allow_equal
    try:
        return os.path.commonpath([target, base]) == base
    except ValueError:  # different drives on Windows
        return False


def _validate_move(move: dict) -> str | None:
    """Return an i18n error key if a client-supplied move is not allowed."""
    if not isinstance(move, dict):
        return "api.invalid_move"
    src_rel = move.get("source_release")
    src_top = move.get("source_top")
    dest = move.get("dest_dir")
    if not all(isinstance(v, str) and v for v in (src_rel, src_top, dest)):
        return "api.missing_paths"
    if move.get("type") not in ("movie", "series", "unknown"):
        return "api.invalid_type"
    if not _is_within(src_top, config["download_dir"]):
        return "api.source_outside"
    if not _is_within(src_rel, src_top, allow_equal=True):
        return "api.release_outside"
    target_root = config["series_dir"] if move["type"] == "series" else config["movies_dir"]
    if not _is_within(dest, target_root):
        return "api.dest_outside"
    base = src_rel if os.path.isdir(src_rel) else os.path.dirname(src_rel)
    for vf in move.get("video_files") or []:
        if not isinstance(vf, str) or not _is_within(os.path.join(base, vf), base):
            return "api.invalid_video_path"
    replaced = move.get("replaced_path")
    if replaced is not None:
        # Deleted by the mover after a successful move → same rules as /api/delete-nas,
        # and it must not contain the new destination (or vice versa)
        if (not isinstance(replaced, str) or not replaced
                or not (_is_within(replaced, config["movies_dir"]) or _is_within(replaced, config["series_dir"]))
                or _is_within(dest, replaced, allow_equal=True) or _is_within(replaced, dest)):
            return "api.invalid_replace"
    return None


# ── Pages & API ───────────────────────────────────────────────────────────────

@app.route("/")
@require_auth
def index():
    # Only hand over what the page needs — never the auth section
    return render_template("index.html", movies_dir=config["movies_dir"],
                           series_dir=config["series_dir"], version=__version__)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "version": __version__})


@app.route("/api/status")
@require_auth
def api_status():
    with _scan_lock:
        scanning = _scan_running
    with _move_lock:
        moving = _move_running
    return jsonify({"ok": True, "scanning": scanning, "moving": moving, "version": __version__})


# Global scan state
_scan_progress: dict = {}
_scan_results:  list = []
_scan_lock = threading.Lock()
_scan_running = False


@app.route("/api/scan", methods=["POST"])
@require_auth
@require_sane_dirs
def api_scan():
    global _scan_running
    with _scan_lock:
        if _scan_running:
            # A scan is already running — the client simply attaches to its stream
            return jsonify({"ok": True, "message": "Scan already running"})
        _scan_running = True
        _scan_progress.clear()
        _scan_progress.update({"phase": "init", "current": "", "found": 0, "done": False,
                               "error": None, "error_i18n": None})
        _scan_results.clear()

    def run():
        global _scan_running
        try:
            def on_progress(state):
                with _scan_lock:
                    _scan_progress.update(state)

            results = scan_downloads(config, on_progress=on_progress)
            with _scan_lock:
                _scan_results.extend(results)
                _scan_progress.update({"phase": "done", "found": len(results), "done": True,
                                       "warnings_i18n": library_warnings(config)})
        except FileNotFoundError as e:
            log.error("Scan failed: %s", e)
            with _scan_lock:
                _scan_progress.update({"phase": "error", "error": str(e), "done": True, "error_i18n": [
                    {"key": "scan.download_dir_missing", "args": {"path": config["download_dir"]}}]})
        except Exception as e:
            log.exception("Scan failed")
            with _scan_lock:
                _scan_progress.update({"phase": "error", "error": str(e), "done": True})
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan started"})


@app.route("/api/scan-progress")
@require_auth
def api_scan_progress():
    def generate():
        while True:
            with _scan_lock:
                state = dict(_scan_progress)
            known_series = state.pop("known_series", [])
            yield f"data: {json.dumps(state)}\n\n"
            if state.get("done"):
                with _scan_lock:
                    results = list(_scan_results)
                final = {"done": True, "items": results, "error": state.get("error"),
                         "error_i18n": state.get("error_i18n"), "known_series": known_series,
                         "warnings_i18n": state.get("warnings_i18n")}
                yield f"data: {json.dumps(final)}\n\n"
                return
            time.sleep(0.15)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/move", methods=["POST"])
@require_auth
@require_sane_dirs
def api_move():
    global _move_running
    data = request.get_json(silent=True) or {}
    moves = data.get("moves") or []
    if not isinstance(moves, list) or not moves:
        return api_error("api.no_moves", 400)

    for move in moves:
        err = _validate_move(move)
        if err:
            log.warning("Refused move %r: %s", move, err)
            return api_error(err, 403)

    unmounted = missing_mount_markers(config)
    if unmounted:
        log.error("Refused move: mount marker %r missing in %s", config["mount_marker"], unmounted)
        return api_error("api.marker_missing", 409, path=unmounted[0], marker=config["mount_marker"])

    with _move_lock:
        if _move_running:
            return api_error("api.move_running", 409)
        _move_running = True
        _move_results.clear()
    mark_pending(len(moves))

    def on_done(results):
        # Runs before the progress state reports "finished" (see execute_moves)
        with _move_lock:
            _move_results.extend(results)
        hist.append(moves, results)

    def run():
        global _move_running
        try:
            execute_moves(moves, config, on_done=on_done)
        except Exception as e:
            log.exception("Move worker crashed")
            mark_failed(str(e))
        finally:
            with _move_lock:
                _move_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "message": "Move started"})


@app.route("/api/progress")
@require_auth
def api_progress():
    def generate():
        while True:
            p = get_progress()
            if not p or not (p.get("active") or p.get("finished")):
                yield f"data: {json.dumps({'active': False, 'finished': False})}\n\n"
                return
            yield f"data: {json.dumps(p)}\n\n"
            if p.get("finished"):
                with _move_lock:
                    results = list(_move_results)
                yield f"data: {json.dumps({'finished': True, 'results': results})}\n\n"
                return
            time.sleep(0.25)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _delete_path(target: str):
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    else:
        os.unlink(target)


@app.route("/api/delete-nas", methods=["POST"])
@require_auth
@require_sane_dirs
def api_delete_nas():
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    if not path or not isinstance(path, str):
        return api_error("api.no_path", 400)

    # Safety: only allow deletion strictly inside movies_dir or series_dir
    if not (_is_within(path, config["movies_dir"]) or _is_within(path, config["series_dir"])):
        log.warning("Refused NAS delete outside media dirs: %s", path)
        return api_error("api.path_outside_media", 403)

    target = os.path.realpath(path)
    if not os.path.lexists(target):
        return api_error("api.path_not_found", 404)
    try:
        _delete_path(target)
        log.info("Deleted NAS path: %s", target)
        return jsonify({"ok": True})
    except Exception as e:
        log.exception("NAS delete failed: %s", target)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/delete", methods=["POST"])
@require_auth
@require_sane_dirs
def api_delete():
    data = request.get_json(silent=True) or {}
    path = data.get("path") or data.get("source_top", "")
    if not path or not isinstance(path, str):
        return api_error("api.no_path", 400)

    # Strictly inside download_dir — never the download_dir itself
    if not _is_within(path, config["download_dir"]):
        log.warning("Refused delete outside download_dir: %s", path)
        return api_error("api.path_outside_downloads", 403)

    target = os.path.realpath(path)
    if not os.path.lexists(target):
        return api_error("api.path_not_found", 404)
    try:
        _delete_path(target)
        log.info("Deleted: %s", target)
        return jsonify({"ok": True})
    except Exception as e:
        log.exception("Delete failed: %s", target)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/history")
@require_auth
def api_history():
    limit = request.args.get("limit", 200, type=int)
    limit = max(1, min(limit, 500))
    return jsonify({"ok": True, "entries": hist.read(limit), "total": hist.count()})


def main():
    parser = argparse.ArgumentParser(description="Folder Mover web UI")
    parser.add_argument("--hash-password", action="store_true",
                        help="print a password hash for auth.password_hash and exit")
    args = parser.parse_args()

    if args.hash_password:
        pw = getpass.getpass("Passwort: ")
        if pw != getpass.getpass("Wiederholen: "):
            raise SystemExit("Passwörter stimmen nicht überein.")
        print(generate_password_hash(pw))
        return

    app.run(host=config.get("host", "0.0.0.0"), port=config.get("port", 8080),
            debug=False, threaded=True)


if __name__ == "__main__":
    main()
