from collections import namedtuple
from pathlib import Path

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


JUNK_RELEASE = ("movie.mkv", "movie.nfo", "Extras/making-of.mkv", "rushchk.log",
                "Rapidgator Premium Account.url", "[---] - ( 4932M 52F - COMPLETE ) - [---]",
                "Links/ddownload.url")


def _move_junk_release(config, dirs):
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], *(f"{rel}/{f}" for f in JUNK_RELEASE))
    dest = dirs["movies_dir"] / "M" / rel
    result = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / rel), "source_top": str(dirs["download_dir"] / rel),
        "dest_dir": str(dest), "type": "movie",
    }], config)[0]
    assert result["ok"], result
    assert not (dirs["download_dir"] / rel).exists()            # junk is deleted with the download
    return result, sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*"))


def test_movie_junk_is_not_copied(config, dirs):
    result, moved = _move_junk_release(config, dirs)
    assert moved == ["Extras", "Extras/making-of.mkv", "movie.mkv", "movie.nfo"]
    assert result["size_bytes"] == 3 * 16
    assert result["warnings"] is None


@pytest.mark.rename
def test_movie_junk_is_removed_after_rename(config, dirs):
    result, moved = _move_junk_release(config, dirs)
    assert result["renamed"]
    assert moved == ["Extras", "Extras/making-of.mkv", "movie.mkv", "movie.nfo"]


def test_junk_patterns_can_be_configured(config, dirs):
    _, moved = _move_junk_release({**config, "junk_patterns": ["*.LOG"]}, dirs)
    assert "rushchk.log" not in moved and "Rapidgator Premium Account.url" in moved


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


def test_interrupted_copy_never_leaves_file_under_final_name(tmp_path):
    # Simulates SIGKILL/OOM/`docker stop` mid-copy: the except-cleanup never runs
    import subprocess
    import sys
    src, dst = tmp_path / "src.mkv", tmp_path / "dst.mkv"
    src.write_bytes(b"x" * (3 * mover.CHUNK_SIZE))
    script = (
        "import os, sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; import mover\n"
        "def cb(name, copied, total, chunk):\n"
        "    if copied >= mover.CHUNK_SIZE: os._exit(9)\n"
        "mover._copy_with_progress(Path(sys.argv[2]), Path(sys.argv[3]), cb)\n"
    )
    root = str(Path(mover.__file__).parent)
    assert subprocess.run([sys.executable, "-c", script, root, str(src), str(dst)]).returncode == 9
    assert not dst.exists()
    assert (tmp_path / "dst.mkv.fmpart").exists()          # clearly unfinished

    # the next attempt replaces the leftover and completes
    mover._copy_with_progress(src, dst, lambda *a: None)
    assert dst.stat().st_size == src.stat().st_size
    assert not (tmp_path / "dst.mkv.fmpart").exists()


def test_movie_folder_is_staged_and_renamed_when_complete(config, dirs, monkeypatch):
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/a.mkv", f"{rel}/b.nfo")
    dest = dirs["movies_dir"] / "M" / rel
    seen = []
    real_copy = mover._copy_with_progress

    def spy(src, dst, cb):
        seen.append(dst.parent.name)
        assert not dest.exists()                             # final name appears only at the end
        real_copy(src, dst, cb)

    monkeypatch.setattr(mover, "_copy_with_progress", spy)
    (dirs["movies_dir"] / "M" / f"{rel}.fmpart").mkdir(parents=True)   # leftover of a crash
    result = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / rel), "source_top": str(dirs["download_dir"] / rel),
        "dest_dir": str(dest), "type": "movie",
    }], config)[0]

    assert result["ok"], result
    assert set(seen) == {f"{rel}.fmpart"}
    assert sorted(p.name for p in dest.iterdir()) == ["a.mkv", "b.nfo"]
    assert not (dirs["movies_dir"] / "M" / f"{rel}.fmpart").exists()


def test_files_appearing_during_copy_are_kept(config, dirs, monkeypatch):
    # e.g. an extractor still writing into the release folder
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/movie.mkv")
    src = dirs["download_dir"] / rel
    real_copy = mover._copy_with_progress
    calls = []

    def copy_then_new_file(s, d, cb):
        real_copy(s, d, cb)
        if not calls:
            calls.append(1)
            (src / "late.mkv").write_bytes(b"y" * 10)

    monkeypatch.setattr(mover, "_copy_with_progress", copy_then_new_file)
    result = mover.execute_moves([{"source_release": str(src), "source_top": str(src),
                                   "dest_dir": str(dirs["movies_dir"] / "M" / rel), "type": "movie"}], config)[0]

    # the release changed during the copy → nothing moved, nothing lost
    assert not result["ok"] and result["error_i18n"][0]["key"] == "mover.source_changed"
    assert (src / "late.mkv").exists() and (src / "movie.mkv").exists()
    assert not (dirs["movies_dir"] / "M" / rel).exists()


def test_files_appearing_after_copy_are_not_deleted(config, dirs, monkeypatch):
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/movie.mkv", f"{rel}/Subs/a.srt")
    src = dirs["download_dir"] / rel
    real_snapshot = mover._snapshot
    calls = []

    def snapshot_then_new_file(path):
        snap = real_snapshot(path)
        calls.append(1)
        if len(calls) == 2:                         # right after the post-copy check
            (src / "Subs" / "late.srt").write_bytes(b"y")
        return snap

    monkeypatch.setattr(mover, "_snapshot", snapshot_then_new_file)
    result = mover.execute_moves([{"source_release": str(src), "source_top": str(src),
                                   "dest_dir": str(dirs["movies_dir"] / "M" / rel), "type": "movie"}], config)[0]

    assert result["ok"] and result["warnings_i18n"][0]["key"] == "mover.source_leftover"
    assert sorted(p.name for p in src.rglob("*")) == ["Subs", "late.srt"]   # only the new file is left
    assert (dirs["movies_dir"] / "M" / rel / "Subs" / "a.srt").exists()


def test_growing_episode_is_not_deleted(config, dirs, monkeypatch):
    rel = "Show.S01E01.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/e.mkv")
    src = dirs["download_dir"] / rel
    real_copy = mover._copy_with_progress

    def copy_while_growing(s, d, cb):
        real_copy(s, d, cb)
        with open(s, "ab") as f:
            f.write(b"more")

    monkeypatch.setattr(mover, "_copy_with_progress", copy_while_growing)
    result = mover.execute_moves([{"source_release": str(src), "source_top": str(src),
                                   "dest_dir": str(dirs["series_dir"] / "Show" / "S01"), "type": "series",
                                   "video_files": ["e.mkv"]}], config)[0]

    assert not result["ok"] and result["error_i18n"][0]["key"] == "mover.file_changed"
    assert (src / "e.mkv").stat().st_size == 20
    assert not (dirs["series_dir"] / "Show" / "S01" / "e.mkv").exists()


def test_episode_folders_with_generic_file_names_do_not_collide(config, dirs):
    rel = "Show.S01.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/Show.S01E01.German/show.mkv", f"{rel}/Show.S01E02.German/show.mkv")
    dest = dirs["series_dir"] / "Show" / "S01"
    result = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / rel), "source_top": str(dirs["download_dir"] / rel),
        "dest_dir": str(dest), "type": "series",
        "video_files": ["Show.S01E01.German/show.mkv", "Show.S01E02.German/show.mkv"],
    }], config)[0]
    assert result["ok"] and not result["warnings"]
    assert sorted(p.name for p in dest.iterdir()) == ["Show.S01E01.German.mkv", "Show.S01E02.German.mkv"]


def test_series_copy_error_midway_is_reported_with_moved_files(config, dirs, monkeypatch):
    import errno
    rel = "Show.S01.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/e01.mkv", f"{rel}/e02.mkv")
    real_copy = mover._copy_with_progress

    def flaky(src, dst, cb):
        if src.name == "e02.mkv":
            raise OSError(errno.ENOSPC, "No space left on device")
        real_copy(src, dst, cb)

    monkeypatch.setattr(mover, "_copy_with_progress", flaky)
    dest = dirs["series_dir"] / "Show" / "S01"
    result = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / rel), "source_top": str(dirs["download_dir"] / rel),
        "dest_dir": str(dest), "type": "series", "video_files": ["e01.mkv", "e02.mkv"],
    }], config)[0]

    assert not result["ok"]
    assert result["error_i18n"][0] == {"key": "mover.copy_failed",
                                       "args": {"name": "e02.mkv", "error": "No space left on device"}}
    assert result["moved_files"] == [str(dest / "e01.mkv")]
    assert (dirs["download_dir"] / rel / "e02.mkv").exists()


def test_series_space_check_also_on_same_device(config, dirs, monkeypatch):
    # Same st_dev, but rename() fails (two bind mounts of one disk): free space is still checked
    rel = "Show.S01E01.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/e.mkv", size=1000)
    monkeypatch.setattr(mover.shutil, "disk_usage", lambda p: namedtuple("U", "total used free")(1, 1, 10))
    result = mover.execute_moves([{
        "source_release": str(dirs["download_dir"] / rel), "source_top": str(dirs["download_dir"] / rel),
        "dest_dir": str(dirs["series_dir"] / "Show" / "S01"), "type": "series", "video_files": ["e.mkv"],
    }], config)[0]
    assert mover._same_device(dirs["download_dir"], dirs["series_dir"])
    assert not result["ok"] and result["error_i18n"][0]["key"] == "mover.no_space"
    assert (dirs["download_dir"] / rel / "e.mkv").exists()


def test_unexpected_error_is_translatable(config, dirs, monkeypatch):
    make_files(dirs["download_dir"], "Movie.2022.German.1080p.WEB-GRP/a.mkv")
    monkeypatch.setattr(mover, "_do_move", lambda *a: 1 / 0)
    result = mover.execute_moves([{"source_release": str(dirs["download_dir"] / "Movie.2022.German.1080p.WEB-GRP"),
                                   "type": "movie"}], config)[0]
    assert result["error_i18n"][0]["key"] == "mover.unexpected"


def _series_move(dirs, rel, video_files):
    src = dirs["download_dir"] / rel
    return {"source_release": str(src), "source_top": str(src), "type": "series",
            "dest_dir": str(dirs["series_dir"] / "Show" / "S01"), "video_files": video_files}


def test_season_pack_sample_is_not_moved_as_episode(config, dirs):
    from scanner import scan_downloads
    rel = "Show.S01.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/show.s01e01.mkv", f"{rel}/Sample/show.s01e01.sample.mkv",
               f"{rel}/show.s01e02-sample.mkv")
    item = scan_downloads(config)[0]
    assert item["suggestion"]["video_files"] == ["show.s01e01.mkv"]

    result = mover.execute_moves([_series_move(dirs, rel, item["suggestion"]["video_files"])], config)[0]
    assert result["ok"]
    assert [p.name for p in (dirs["series_dir"] / "Show" / "S01").iterdir()] == ["show.s01e01.mkv"]
    assert not (dirs["download_dir"] / rel).exists()          # only the sample was left → cleaned up


def test_subtitles_are_moved_with_their_episode(config, dirs):
    rel = "Show.S01.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/show.s01e01.mkv", f"{rel}/show.s01e01.de.srt",
               f"{rel}/Subs/show.s01e01.en.forced.srt", f"{rel}/show.s01e02.mkv", f"{rel}/show.nfo")
    result = mover.execute_moves([_series_move(dirs, rel, ["show.s01e01.mkv", "show.s01e02.mkv"])], config)[0]
    assert result["ok"]
    assert sorted(p.name for p in (dirs["series_dir"] / "Show" / "S01").iterdir()) == [
        "show.s01e01.de.srt", "show.s01e01.en.forced.srt", "show.s01e01.mkv", "show.s01e02.mkv"]


def test_single_episode_takes_generic_subtitles(config, dirs):
    rel = "Show.S01E01.German.1080p.WEB-GRP"
    make_files(dirs["download_dir"], f"{rel}/{rel}.mkv", f"{rel}/Subs/English.srt")
    assert mover.execute_moves([_series_move(dirs, rel, [f"{rel}.mkv"])], config)[0]["ok"]
    assert (dirs["series_dir"] / "Show" / "S01" / f"{rel}.English.srt").exists()


def test_loose_episode_does_not_take_unrelated_subtitles(config, dirs):
    make_files(dirs["download_dir"], "Show.S01E01.mkv", "Show.S01E01.de.srt", "Other.Movie.srt")
    move = {"source_release": str(dirs["download_dir"] / "Show.S01E01.mkv"),
            "source_top": str(dirs["download_dir"] / "Show.S01E01.mkv"), "type": "series", "is_loose_file": True,
            "dest_dir": str(dirs["series_dir"] / "Show" / "S01"), "video_files": ["Show.S01E01.mkv"]}
    assert mover.execute_moves([move], config)[0]["ok"]
    assert sorted(p.name for p in (dirs["series_dir"] / "Show" / "S01").iterdir()) == [
        "Show.S01E01.de.srt", "Show.S01E01.mkv"]
    assert (dirs["download_dir"] / "Other.Movie.srt").exists()
