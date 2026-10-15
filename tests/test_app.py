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
        sess["logged_in"] = True
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
