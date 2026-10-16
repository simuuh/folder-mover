import json
import time

import pytest
from conftest import UUID_A, make_files
from werkzeug.security import generate_password_hash

import app as app_module
import history


@pytest.fixture
def client(config, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "config", {
        **config, "auth": {"username": "admin", "password": "s3cret", "password_hash": ""},
    })
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history.jsonl")
    app_module._login_failures.clear()
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def auth_client(client):
    with client.session_transaction() as sess:
        app_module.start_session(sess)
    return client


def test_requires_login(client):
    assert client.get("/").status_code == 302
    assert client.post("/api/scan").status_code == 401


def test_healthz_is_public(client):
    assert client.get("/healthz").get_json()["ok"]


def test_security_headers(client):
    resp = client.get("/login")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]


def test_login_plaintext(client):
    assert client.post("/login", data={"username": "admin", "password": "wrong"}).status_code == 200
    resp = client.post("/login", data={"username": "admin", "password": "s3cret"})
    assert resp.status_code == 302 and resp.location.endswith("/")


def test_login_password_hash(client, monkeypatch):
    app_module.config["auth"] = {"username": "admin", "password": "",
                                 "password_hash": generate_password_hash("hashed!")}
    assert client.post("/login", data={"username": "admin", "password": "hashed!"}).status_code == 302


def test_login_rate_limit(client):
    for _ in range(app_module.LOGIN_MAX_FAILURES):
        client.post("/login", data={"username": "admin", "password": "nope"})
    resp = client.post("/login", data={"username": "admin", "password": "s3cret"})
    assert resp.status_code == 429


def _move(dirs, **override):
    rel = "Movie.2022.German.1080p.WEB-GRP"
    move = {
        "source_release": str(dirs["download_dir"] / rel),
        "source_top": str(dirs["download_dir"] / rel),
        "dest_dir": str(dirs["movies_dir"] / "M" / rel),
        "type": "movie",
    }
    move.update(override)
    return move


@pytest.mark.parametrize("override", [
    {"dest_dir": "/etc/evil"},
    {"type": "series"},                                      # dest not in series_dir
    {"source_top": "/", "source_release": "/etc"},
    {"type": "bogus"},
    {"video_files": ["../../outside.mkv"]},
])
def test_move_rejects_paths_outside_config(auth_client, dirs, override):
    move = _move(dirs, **override)
    resp = auth_client.post("/api/move", json={"moves": [move]})
    assert resp.status_code in (400, 403), resp.get_json()


def test_move_rejects_traversal_in_series_name(auth_client, dirs):
    move = _move(dirs, type="series", dest_dir=str(dirs["series_dir"] / ".." / ".." / "x" / "S01"))
    assert auth_client.post("/api/move", json={"moves": [move]}).status_code == 403


def test_move_end_to_end_and_history(auth_client, dirs):
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/movie.mkv")
    resp = auth_client.post("/api/move", json={"moves": [_move(dirs)]})
    assert resp.get_json()["ok"]

    for _ in range(100):
        if not auth_client.get("/api/status").get_json()["moving"]:
            break
        time.sleep(0.05)

    assert (dirs["movies_dir"] / "M" / "Movie.2022.German.1080p.WEB-GRP" / "movie.mkv").exists()
    entries = auth_client.get("/api/history").get_json()["entries"]
    assert entries[0]["ok"] and entries[0]["size_bytes"] == 16


def test_history_written_before_move_reports_finished(auth_client, dirs, monkeypatch):
    # The browser reloads the history as soon as the progress stream says
    # "finished" — so the history must already be written at that point.
    real_append = history.append

    def slow_append(*args):
        time.sleep(0.5)
        real_append(*args)

    monkeypatch.setattr(app_module.hist, "append", slow_append)
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/movie.mkv")
    assert auth_client.post("/api/move", json={"moves": [_move(dirs)]}).get_json()["ok"]

    body = auth_client.get("/api/progress").get_data(as_text=True)
    final = json.loads([line for line in body.splitlines() if line.startswith("data:")][-1][5:])
    assert final["results"] and final["results"][0]["ok"]

    data = auth_client.get("/api/history").get_json()
    assert data["total"] == 1 and len(data["entries"]) == 1


def test_history_total_is_not_capped_by_limit(auth_client):
    history.append([{"source_release": f"/x/{i}"} for i in range(3)], [{"ok": True}] * 3)
    data = auth_client.get("/api/history?limit=2").get_json()
    assert len(data["entries"]) == 2 and data["total"] == 3


def test_scan_via_sse(auth_client, dirs):
    make_files(dirs["download_dir"], f"{UUID_A}/Movie.A.2020.1080p.WEB-GRP/a.mkv")
    assert auth_client.post("/api/scan").get_json()["ok"]
    body = auth_client.get("/api/scan-progress").get_data(as_text=True)
    final = json.loads([line for line in body.splitlines() if line.startswith("data:")][-1][5:])
    assert final["done"] and final["error"] is None
    assert [i["release_name"] for i in final["items"]] == ["Movie.A.2020.1080p.WEB-GRP"]


def test_scan_error_is_reported_in_final_event(auth_client, dirs):
    app_module.config["download_dir"] = str(dirs["download_dir"] / "missing")
    auth_client.post("/api/scan")
    body = auth_client.get("/api/scan-progress").get_data(as_text=True)
    final = json.loads([line for line in body.splitlines() if line.startswith("data:")][-1][5:])
    assert final["done"] and "not found" in final["error"]


def test_delete_refuses_download_dir_itself(auth_client, dirs):
    resp = auth_client.post("/api/delete", json={"path": str(dirs["download_dir"])})
    assert resp.status_code == 403
    assert dirs["download_dir"].exists()


def test_delete_refuses_outside(auth_client, dirs):
    resp = auth_client.post("/api/delete", json={"path": str(dirs["download_dir"] / ".." / "Filme")})
    assert resp.status_code == 403
    assert dirs["movies_dir"].exists()


def test_delete_inside(auth_client, dirs):
    make_files(dirs["download_dir"], "Junk/file.mkv")
    resp = auth_client.post("/api/delete", json={"path": str(dirs["download_dir"] / "Junk")})
    assert resp.get_json()["ok"]
    assert not (dirs["download_dir"] / "Junk").exists()


def test_delete_nas_refuses_media_root(auth_client, dirs):
    resp = auth_client.post("/api/delete-nas", json={"path": str(dirs["movies_dir"])})
    assert resp.status_code == 403


def test_history_limit_param_is_robust(auth_client):
    assert auth_client.get("/api/history?limit=abc").status_code == 200


def test_logout_requires_post(auth_client):
    assert auth_client.get("/logout").status_code == 405
    assert auth_client.post("/logout").status_code == 302
    assert auth_client.post("/api/scan").status_code == 401


def test_index_does_not_leak_password(auth_client):
    assert "s3cret" not in auth_client.get("/").get_data(as_text=True)


def test_check_config_warns_about_overlap(config):
    from config import check_config
    config["movies_dir"] = config["download_dir"]
    assert any("overlap" in p for p in check_config(config))


def test_env_overrides_config_file(tmp_path, monkeypatch):
    from config import load_config
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("download_dir: /from/file\nport: 1234\nauth: {username: fileuser}\n", encoding="utf-8")
    monkeypatch.setenv("FOLDER_MOVER_DOWNLOAD_DIR", "/downloads")
    monkeypatch.setenv("FOLDER_MOVER_PORT", "9090")
    monkeypatch.setenv("FOLDER_MOVER_PASSWORD_HASH", "scrypt:abc")
    cfg = load_config(str(cfg_file))
    assert cfg["download_dir"] == "/downloads" and cfg["port"] == 9090
    assert cfg["auth"] == {"username": "fileuser", "password": "changeme", "password_hash": "scrypt:abc"}


def test_secret_key_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history.jsonl")
    first = app_module._load_secret_key({})
    assert (tmp_path / ".secret_key").read_text(encoding="utf-8") == first
    assert app_module._load_secret_key({}) == first
    assert app_module._load_secret_key({"secret_key": "fixed"}) == "fixed"


def _wait_for_move(client):
    for _ in range(100):
        if not client.get("/api/status").get_json()["moving"]:
            return
        time.sleep(0.05)


def test_replace_deletes_old_release_only_after_successful_move(auth_client, dirs):
    old = dirs["movies_dir"] / "M" / "Movie.2022.German.720p.WEB-OLD"
    make_files(old, "old.mkv")
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/movie.mkv")
    resp = auth_client.post("/api/move", json={"moves": [_move(dirs, replaced_path=str(old))]})
    assert resp.get_json()["ok"]
    _wait_for_move(auth_client)

    assert (dirs["movies_dir"] / "M" / "Movie.2022.German.1080p.WEB-GRP" / "movie.mkv").exists()
    assert not old.exists()
    assert auth_client.get("/api/history").get_json()["entries"][0]["replaced_path"] == str(old)


def test_replace_keeps_old_release_when_move_fails(auth_client, dirs):
    old = dirs["movies_dir"] / "M" / "Movie.2022.German.720p.WEB-OLD"
    make_files(old, "old.mkv")
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/movie.mkv")
    (dirs["movies_dir"] / "M" / "Movie.2022.German.1080p.WEB-GRP").mkdir()      # → "destination exists"
    assert auth_client.post("/api/move", json={"moves": [_move(dirs, replaced_path=str(old))]}).get_json()["ok"]
    _wait_for_move(auth_client)

    assert (old / "old.mkv").exists()
    entry = auth_client.get("/api/history").get_json()["entries"][0]
    assert not entry["ok"] and entry["replaced_path"] is None


@pytest.mark.parametrize("replaced", [
    "/etc",                                     # outside the media dirs
    "{movies}",                                 # the media root itself
    "{movies}/M",                               # contains the new destination
    "{movies}/M/Movie.2022.German.1080p.WEB-GRP/sub",   # inside the new destination
    "{downloads}/Movie.2022.German.1080p.WEB-GRP",      # the source
])
def test_replace_path_is_validated(auth_client, dirs, replaced):
    replaced = replaced.format(movies=dirs["movies_dir"], downloads=dirs["download_dir"])
    resp = auth_client.post("/api/move", json={"moves": [_move(dirs, replaced_path=replaced)]})
    assert resp.status_code == 403 and resp.get_json()["error_key"] == "api.invalid_replace"


@pytest.mark.parametrize("endpoint, body", [
    ("/api/scan", {}),
    ("/api/move", {"moves": [{}]}),
    ("/api/delete", {"path": "x"}),
    ("/api/delete-nas", {"path": "x"}),
])
def test_overlapping_dirs_block_file_operations(auth_client, dirs, endpoint, body):
    # e.g. download_dir=/data, movies_dir=/data/Filme → the library would show up as "downloads"
    app_module.config["movies_dir"] = str(dirs["download_dir"] / "Filme")
    resp = auth_client.post(endpoint, json=body)
    assert resp.status_code == 409 and resp.get_json()["error_key"] == "api.dirs_overlap"


def test_overlap_detection(config, tmp_path):
    from config import dir_overlaps
    assert dir_overlaps(config) == []
    assert dir_overlaps({**config, "series_dir": config["movies_dir"]}) == [("movies_dir", "series_dir")]
    assert dir_overlaps({**config, "download_dir": str(tmp_path)}) == [("download_dir", "movies_dir"),
                                                                        ("download_dir", "series_dir")]
    # a common prefix is no overlap
    assert dir_overlaps({**config, "series_dir": config["movies_dir"] + "2"}) == []


def test_mount_marker_blocks_moves_into_unmounted_share(auth_client, dirs):
    # NAS not mounted → movies_dir is just the empty local mountpoint
    app_module.config["mount_marker"] = ".folder-mover"
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/movie.mkv")
    resp = auth_client.post("/api/move", json={"moves": [_move(dirs)]})
    assert resp.status_code == 409 and resp.get_json()["error_key"] == "api.marker_missing"
    assert (dirs["download_dir"] / "Movie.2022.German.1080p.WEB-GRP" / "movie.mkv").exists()

    for d in ("movies_dir", "series_dir"):
        (dirs[d] / ".folder-mover").touch()
    assert auth_client.post("/api/move", json={"moves": [_move(dirs)]}).get_json()["ok"]


def test_mover_rechecks_marker_per_item(config, dirs):
    import mover
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/movie.mkv")
    result = mover.execute_moves([_move(dirs)], {**config, "mount_marker": ".folder-mover"})[0]
    assert not result["ok"] and result["error_i18n"][0]["key"] == "mover.marker_missing"
    assert not (dirs["movies_dir"] / "M").exists()


def test_scan_warns_about_empty_or_unmarked_library(auth_client, dirs):
    make_files(dirs["series_dir"], "Some Show/S01/e.mkv")             # movies_dir stays empty
    auth_client.post("/api/scan")
    body = auth_client.get("/api/scan-progress").get_data(as_text=True)
    final = json.loads([line for line in body.splitlines() if line.startswith("data:")][-1][5:])
    assert final["warnings_i18n"] == [{"key": "scan.library_empty", "args": {"path": str(dirs["movies_dir"])}}]

    app_module.config["mount_marker"] = ".folder-mover"
    (dirs["series_dir"] / ".folder-mover").touch()
    from config import library_warnings
    assert [w["key"] for w in library_warnings(app_module.config)] == ["scan.marker_missing"]


@pytest.mark.parametrize("password", ["changeme", ""])
def test_default_or_empty_password_disables_login(client, password):
    app_module.config["auth"] = {"username": "admin", "password": password, "password_hash": ""}
    page = client.get("/login")
    assert page.status_code == 403 and "--hash-password" in page.get_data(as_text=True)
    resp = client.post("/login", data={"username": "admin", "password": password})
    assert resp.status_code == 403
    assert client.get("/api/status").status_code == 401


def test_hash_allows_login_even_if_plaintext_is_default(client):
    app_module.config["auth"] = {"username": "admin", "password": "changeme",
                                 "password_hash": generate_password_hash("hashed!")}
    assert client.post("/login", data={"username": "admin", "password": "hashed!"}).status_code == 302


def _login(client):
    assert client.post("/login", data={"username": "admin", "password": "s3cret"}).status_code == 302
    return client.get_cookie("session").value


def test_session_cookie_is_invalid_after_logout(client):
    cookie = _login(client)
    client.post("/logout")
    client.set_cookie("session", cookie)            # replay a copied cookie
    assert client.get("/api/status").status_code == 401


def test_session_ends_on_password_change(client):
    _login(client)
    assert client.get("/api/status").status_code == 200
    app_module.config["auth"]["password"] = "a-new-password"
    assert client.get("/api/status").status_code == 401


def test_session_expires(client, monkeypatch):
    _login(client)
    monkeypatch.setitem(app_module.config, "session_days", 1)
    real_time = time.time
    monkeypatch.setattr(app_module.time, "time", lambda: real_time() + 2 * 86400)
    assert client.get("/api/status").status_code == 401


def test_old_style_session_is_rejected(client):
    with client.session_transaction() as sess:
        sess["logged_in"] = True                    # cookie of an older version
    assert client.get("/api/status").status_code == 401


def test_successful_login_resets_failures(client):
    for _ in range(app_module.LOGIN_MAX_FAILURES - 1):
        client.post("/login", data={"username": "admin", "password": "nope"})
    _login(client)
    client.post("/logout")
    for _ in range(app_module.LOGIN_MAX_FAILURES - 1):
        client.post("/login", data={"username": "admin", "password": "nope"})
    assert client.post("/login", data={"username": "admin", "password": "s3cret"}).status_code == 302


def test_empty_secret_key_file_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history.jsonl")
    (tmp_path / ".secret_key").write_text("")
    key = app_module._load_secret_key({})
    assert len(key) == 64 and (tmp_path / ".secret_key").read_text(encoding="utf-8") == key


def test_proxy_count_is_read_as_int(tmp_path, monkeypatch):
    from config import load_config
    monkeypatch.setenv("FOLDER_MOVER_PROXY_COUNT", "1")
    assert load_config(str(tmp_path / "missing.yaml"))["proxy_count"] == 1


def test_scan_progress_without_scan_ends_immediately(auth_client, monkeypatch):
    monkeypatch.setattr(app_module, "_scan_progress", {})
    body = auth_client.get("/api/scan-progress").get_data(as_text=True)   # would hang before
    final = json.loads(body.splitlines()[0][5:])
    assert final == {"done": True, "items": [], "error": None}


@pytest.mark.parametrize("endpoint", ["/api/delete", "/api/delete-nas"])
def test_delete_is_refused_while_moving(auth_client, dirs, monkeypatch, endpoint):
    make_files(dirs["download_dir"], "Junk/a.mkv")
    monkeypatch.setattr(app_module, "_move_running", True)
    resp = auth_client.post(endpoint, json={"path": str(dirs["download_dir"] / "Junk")})
    assert resp.status_code == 409 and resp.get_json()["error_key"] == "api.move_running"
    assert (dirs["download_dir"] / "Junk" / "a.mkv").exists()
