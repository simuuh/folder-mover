"""Jellyfin/Plex library refresh after a move (mediaserver.py)."""

import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import make_files
from test_app import _move, auth_client, client  # noqa: F401  (fixtures)

import app as app_module
import mediaserver
from config import DEFAULT_CONFIG, load_config


@pytest.fixture
def fake_server():
    """A local HTTP server recording requests; `status` sets the reply code."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        status = 204

        def _handle(self):
            calls.append({"method": self.command, "path": self.path, "headers": dict(self.headers)})
            self.send_response(Handler.status)
            self.end_headers()

        do_GET = do_POST = _handle

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", calls, Handler
    server.shutdown()


def test_nothing_configured():
    assert mediaserver.configured(DEFAULT_CONFIG) == []
    assert mediaserver.refresh(DEFAULT_CONFIG) == {}
    assert mediaserver.refresh_in_background(DEFAULT_CONFIG) is None


def test_url_without_key_is_ignored():
    assert mediaserver.configured({"jellyfin": {"url": "http://x", "api_key": ""},
                                   "plex": {"url": "", "token": "t"}}) == []


def test_jellyfin_and_plex_requests(fake_server):
    url, calls, _ = fake_server
    cfg = {"jellyfin": {"url": url + "/", "api_key": "jf-key"}, "plex": {"url": url, "token": "plex-token"}}

    assert mediaserver.refresh(cfg) == {"Jellyfin": None, "Plex": None}

    jellyfin, plex = calls
    assert jellyfin["method"] == "POST" and jellyfin["path"] == "/Library/Refresh"
    assert jellyfin["headers"]["Authorization"] == 'MediaBrowser Token="jf-key"'
    assert plex["method"] == "GET" and plex["path"] == "/library/sections/all/refresh"
    assert plex["headers"]["X-Plex-Token"] == "plex-token"
    assert "plex-token" not in plex["path"]   # never in the URL (access logs)


def test_errors_are_reported_not_raised(fake_server):
    url, _, handler = fake_server
    handler.status = 401
    outcome = mediaserver.refresh({"jellyfin": {"url": url, "api_key": "wrong"},
                                   "plex": {"url": "http://127.0.0.1:1", "token": "t"}})
    assert outcome["Jellyfin"] == "HTTP 401"
    assert outcome["Plex"]   # connection refused


def test_env_overrides_nested_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLDER_MOVER_CONFIG", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("FOLDER_MOVER_JELLYFIN_URL", "http://jf:8096")
    monkeypatch.setenv("FOLDER_MOVER_JELLYFIN_API_KEY", "k")
    cfg = load_config()
    assert cfg["jellyfin"] == {"url": "http://jf:8096", "api_key": "k"}
    assert DEFAULT_CONFIG["jellyfin"] == {"url": "", "api_key": ""}   # defaults not mutated


def _wait_until_idle(test_client):
    for _ in range(100):
        if not test_client.get("/api/status").get_json()["moving"]:
            return
        time.sleep(0.05)


def test_refresh_after_successful_move(auth_client, dirs, monkeypatch):  # noqa: F811
    triggered = []
    monkeypatch.setattr(mediaserver, "refresh_in_background", triggered.append)
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/movie.mkv")
    assert auth_client.post("/api/move", json={"moves": [_move(dirs)]}).get_json()["ok"]
    _wait_until_idle(auth_client)
    assert triggered == [app_module.config]


def test_no_refresh_when_nothing_moved(auth_client, dirs, monkeypatch):  # noqa: F811
    triggered = []
    monkeypatch.setattr(mediaserver, "refresh_in_background", triggered.append)
    # Source does not exist → the move fails
    assert auth_client.post("/api/move", json={"moves": [_move(dirs)]}).get_json()["ok"]
    _wait_until_idle(auth_client)
    entries = auth_client.get("/api/history").get_json()["entries"]
    assert entries and not entries[0]["ok"]
    assert triggered == []


def test_refresh_never_breaks_a_move(monkeypatch):
    def boom(*args):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(mediaserver, "_request", boom)
    cfg = {"jellyfin": {"url": "http://jf", "api_key": "k"}}
    thread = mediaserver.refresh_in_background(cfg)
    thread.join(2)
    assert not thread.is_alive()
    assert json.dumps(mediaserver.refresh(cfg)) == '{"Jellyfin": "down"}'
