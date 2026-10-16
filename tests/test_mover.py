from collections import namedtuple

import pytest
from conftest import UUID_A, make_files

import mover


@pytest.fixture(autouse=True)
def force_copy(request, monkeypatch):
    """tmp_path is one filesystem → would always rename. Test the copy path unless marked."""
    if not request.node.get_closest_marker("rename"):
        monkeypatch.setattr(mover, "_try_rename", lambda src, dst: False)


@pytest.mark.rename
def test_same_filesystem_movie_is_renamed(config, dirs, monkeypatch):
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{UUID_A}/{rel}/movie.mkv")
    monkeypatch.setattr(mover, "_copy_with_progress", lambda *a: pytest.fail("must not copy"))
    dest = dirs["movies_dir"] / "M" / rel

    results = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / UUID_A / rel),
        "source_top": str(dirs["download_dir"] / UUID_A), "dest_dir": str(dest), "type": "movie",
    }], config)

    assert results[0]["ok"] and results[0]["renamed"]
    assert (dest / "movie.mkv").exists()
    assert not (dirs["download_dir"] / UUID_A).exists()


@pytest.mark.rename
def test_same_filesystem_episode_is_renamed(config, dirs, monkeypatch):
    fname = "Show.S01E01.German.1080p.WEB-GRP.mkv"
    make_files(dirs["download_dir"], fname)
    monkeypatch.setattr(mover, "_copy_with_progress", lambda *a: pytest.fail("must not copy"))
    dest = dirs["series_dir"] / "Show" / "S01"

    results = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / fname), "source_top": str(dirs["download_dir"] / fname),
        "dest_dir": str(dest), "type": "series", "video_files": [fname], "is_loose_file": True,
    }], config)

    assert results[0]["ok"] and (dest / fname).exists()


@pytest.mark.rename
def test_cross_device_rename_falls_back_to_copy(config, dirs, monkeypatch):
    import errno

    def exdev(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(mover.os, "rename", exdev)
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/movie.mkv")
    dest = dirs["movies_dir"] / "M" / rel

    results = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / rel), "source_top": str(dirs["download_dir"] / rel),
        "dest_dir": str(dest), "type": "movie",
    }], config)

    assert results[0]["ok"] and "renamed" not in results[0]
    assert (dest / "movie.mkv").exists() and not (dirs["download_dir"] / rel).exists()


def test_move_movie_folder(config, dirs):
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{UUID_A}/{rel}/movie.mkv", f"{UUID_A}/{rel}/info.nfo")
    src = dirs["download_dir"] / UUID_A / rel
    dest = dirs["movies_dir"] / "M" / rel

    results = mover.execute_moves([{
        "source_release": str(src), "source_top": str(dirs["download_dir"] / UUID_A),
        "dest_dir": str(dest), "type": "movie",
    }], config)

    assert results[0]["ok"], results
    assert (dest / "movie.mkv").read_bytes() == b"x" * 16
    assert (dest / "info.nfo").exists()
    assert not (dirs["download_dir"] / UUID_A).exists()   # empty wrapper cleaned up
    assert results[0]["size_bytes"] == 32
    assert mover.get_progress()["finished"]


def test_move_series_loose_file_in_wrapper(config, dirs):
    fname = "The.Boys.S05E01.German.1080p.WEB-GRP.mkv"
    make_files(dirs["download_dir"], f"{UUID_A}/{fname}")
    dest = dirs["series_dir"] / "The Boys" / "S05"

    results = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / UUID_A / fname),
        "source_top": str(dirs["download_dir"] / UUID_A),
        "dest_dir": str(dest), "type": "series", "video_files": [fname], "is_loose_file": True,
    }], config)

    assert results[0]["ok"], results
    assert (dest / fname).exists()
    assert not (dirs["download_dir"] / UUID_A).exists()


def test_shared_wrapper_sibling_survives(config, dirs):
    make_files(dirs["download_dir"],
               f"{UUID_A}/Movie.A.2020.1080p.WEB-GRP/a.mkv",
               f"{UUID_A}/Movie.B.2021.1080p.WEB-GRP/b.mkv")
    top = dirs["download_dir"] / UUID_A

    results = mover.execute_moves([{
        "source_release": str(top / "Movie.A.2020.1080p.WEB-GRP"), "source_top": str(top),
        "dest_dir": str(dirs["movies_dir"] / "M" / "Movie.A.2020.1080p.WEB-GRP"), "type": "movie",
    }], config)

    assert results[0]["ok"]
    assert (top / "Movie.B.2021.1080p.WEB-GRP" / "b.mkv").exists()


def test_existing_destination_is_not_overwritten(config, dirs):
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/movie.mkv")
    make_files(dirs["movies_dir"], f"M/{rel}/movie.mkv", size=3)

    results = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / rel), "source_top": str(dirs["download_dir"] / rel),
        "dest_dir": str(dirs["movies_dir"] / "M" / rel), "type": "movie",
    }], config)

    assert not results[0]["ok"]
    assert (dirs["movies_dir"] / "M" / rel / "movie.mkv").read_bytes() == b"xxx"
    assert (dirs["download_dir"] / rel / "movie.mkv").exists()


def test_not_enough_space(config, dirs, monkeypatch):
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/movie.mkv", size=1000)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(mover.shutil, "disk_usage", lambda p: Usage(10, 10, 10))

    results = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / rel), "source_top": str(dirs["download_dir"] / rel),
        "dest_dir": str(dirs["movies_dir"] / "M" / rel), "type": "movie",
    }], config)

    assert not results[0]["ok"]
    assert "free space" in results[0]["error"]
    assert (dirs["download_dir"] / rel / "movie.mkv").exists()


def test_failed_copy_removes_partial_file(tmp_path, monkeypatch):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 100)
    dst = tmp_path / "dst.mkv"

    def boom(*args):
        raise OSError("NAS gone")

    with pytest.raises(OSError):
        mover._copy_with_progress(src, dst, boom)
    assert not dst.exists()
    assert src.exists()


def test_failed_movie_copy_rolls_back_and_keeps_source(config, dirs, monkeypatch):
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/a.mkv", f"{rel}/b.nfo", f"{rel}/c.srt")
    real_copy = mover._copy_with_progress
    calls = []

    def flaky_copy(src, dst, cb):
        calls.append(src.name)
        if len(calls) == 2:
            raise OSError("NAS gone")
        real_copy(src, dst, cb)

    monkeypatch.setattr(mover, "_copy_with_progress", flaky_copy)
    dest = dirs["movies_dir"] / "M" / rel
    results = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / rel), "source_top": str(dirs["download_dir"] / rel),
        "dest_dir": str(dest), "type": "movie",
    }], config)

    assert not results[0]["ok"]
    assert not dest.exists()                                   # rolled back
    assert sorted(p.name for p in (dirs["download_dir"] / rel).iterdir()) == ["a.mkv", "b.nfo", "c.srt"]


def test_series_progress_counts_only_video_files(dirs):
    rel = dirs["download_dir"] / "Show.S01.German.1080p.WEB-GRP"
    make_files(rel, "e01.mkv", "info.nfo", size=10)
    assert mover._calc_size({"source_release": str(rel), "type": "series", "video_files": ["e01.mkv"]}) == 10
