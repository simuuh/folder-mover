from collections import namedtuple

import pytest
from conftest import UUID_A, make_files

import mover


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
