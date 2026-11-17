"""Guards against hanging NAS mounts (nas.py) and how scan/move use them."""

import json
import threading
import time
from collections import namedtuple

import pytest
from conftest import make_files
from test_app import _move, auth_client, client  # noqa: F401  (fixtures)

import app as app_module
import mover
import nas


@pytest.fixture
def hanging_series_dir(dirs, monkeypatch):
    """series_dir behaves like a dead mount: every probe of it blocks."""
    release = threading.Event()
    real_probe = nas._probe

    def probe(path):
        if path == str(dirs["series_dir"]):
            release.wait(10)
        else:
            real_probe(path)

    monkeypatch.setattr(nas, "_probe", probe)
    monkeypatch.setattr(nas, "_probes", {})
    yield str(dirs["series_dir"])
    release.set()   # let the blocked probe thread end


def test_probe_is_off_without_timeout(config, hanging_series_dir):
    assert nas.unresponsive_dirs({**config, "nas_timeout": 0}) == []


def test_responsive_dirs_pass(config, monkeypatch):
    monkeypatch.setattr(nas, "_probes", {})
    assert nas.unresponsive_dirs({**config, "nas_timeout": 5}) == []


def test_hanging_dir_is_reported_and_probe_reused(config, hanging_series_dir):
    cfg = {**config, "nas_timeout": 0.2}
    assert nas.unresponsive_dirs(cfg) == [hanging_series_dir]
    probe_thread = nas._probes[hanging_series_dir][0]

    start = time.monotonic()
    assert nas.unresponsive_dirs(cfg) == [hanging_series_dir]
    # The still-blocked probe is reused (no new stuck thread) and not waited for again
    assert nas._probes[hanging_series_dir][0] is probe_thread
    assert time.monotonic() - start < 0.15


def test_stall_watch_reports_stall_and_recovery():
    calls = []
    with nas.StallWatch("Test", 0.2, calls.append, interval=0.05) as watch:
        time.sleep(0.4)
        assert calls and calls[-1] >= 1
        watch.touch()
        time.sleep(0.15)
        assert calls[-1] == 0


def test_stall_watch_off_without_timeout():
    calls = []
    with nas.StallWatch("Test", 0, calls.append, interval=0.01):
        time.sleep(0.05)
    assert calls == []


def test_scan_refused_when_nas_hangs(auth_client, hanging_series_dir):  # noqa: F811
    app_module.config["nas_timeout"] = 0.2
    auth_client.post("/api/scan")
    body = auth_client.get("/api/scan-progress").get_data(as_text=True)
    final = json.loads([line for line in body.splitlines() if line.startswith("data:")][-1][5:])
    assert final["done"] and final["error_i18n"][0]["key"] == "scan.nas_unresponsive"
    assert final["error_i18n"][0]["args"]["path"] == hanging_series_dir


def test_move_refused_when_nas_hangs(auth_client, dirs, hanging_series_dir):  # noqa: F811
    app_module.config["nas_timeout"] = 0.2
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/movie.mkv")
    resp = auth_client.post("/api/move", json={"moves": [_move(dirs)]})
    assert resp.status_code == 503 and resp.get_json()["error_key"] == "api.nas_unresponsive"
    assert (dirs["download_dir"] / "Movie.2022.German.1080p.WEB-GRP" / "movie.mkv").exists()


def test_mover_rechecks_nas_per_item(config, dirs, hanging_series_dir):
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/movie.mkv")
    result = mover.execute_moves([_move(dirs)], {**config, "nas_timeout": 0.2})[0]
    assert not result["ok"] and result["error_i18n"][0]["key"] == "mover.nas_unresponsive"
    assert not (dirs["movies_dir"] / "M").exists()


def test_stalled_copy_is_flagged_in_progress(config, dirs, monkeypatch):
    seen = []

    def stuck_copy(src, dst, callback):
        # Simulate a write that blocks on the NAS until the watchdog notices it
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not mover.get_progress().get("stalled_seconds"):
            time.sleep(0.05)
        seen.append(mover.get_progress())
        dst.write_bytes(src.read_bytes())

    monkeypatch.setattr(mover, "_copy_with_progress", stuck_copy)
    monkeypatch.setattr(mover, "_try_rename", lambda src, dst: False)
    monkeypatch.setattr(nas, "_probes", {})
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/movie.mkv")

    result = mover.execute_moves([_move(dirs)], {**config, "nas_timeout": 0.3})[0]

    assert result["ok"]
    assert seen[0]["stalled_seconds"] >= 1 and seen[0]["speed_bps"] == 0
    assert mover.get_progress()["stalled_seconds"] == 0


def test_disk_space_merges_same_filesystem(config, monkeypatch):
    monkeypatch.setattr(nas, "_probes", {})
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(nas.shutil, "disk_usage", lambda p: usage(1000 * 1024**3, 950 * 1024**3, 50 * 1024**3))

    targets = nas.disk_space({**config, "space_warning_gb": 100, "nas_timeout": 5})

    assert len(targets) == 1 and targets[0]["keys"] == ["movies_dir", "series_dir"]   # tmp_path: one device
    assert targets[0]["free"] == 50 * 1024**3 and targets[0]["low"]
    assert not nas.disk_space({**config, "space_warning_gb": 0})[0]["low"]


def test_disk_space_reports_missing_and_hanging(config, dirs, hanging_series_dir):
    dirs["movies_dir"].rmdir()
    targets = {t["keys"][0]: t for t in nas.disk_space({**config, "nas_timeout": 0.2})}
    assert targets["movies_dir"]["missing"]
    assert targets["series_dir"]["unresponsive"]


def test_space_api(auth_client):  # noqa: F811
    data = auth_client.get("/api/space").get_json()
    assert data["ok"] and data["targets"][0]["total"] > 0
