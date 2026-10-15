import inspect
from pathlib import Path

import pytest
from conftest import UUID_A, UUID_B, make_files

import scanner

EXTS = {".mkv", ".mp4"}


def names(paths):
    return [p.name for p in paths]


# ── classify ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, typ, extra", [
    ("The.Boys.S05E03.German.1080p.WEB-GRP", "series", {"season": 5, "episodes": [3]}),
    ("The.Boys.S05.German.1080p.WEB-GRP", "series", {"season": 5, "is_season_pack": True}),
    ("Show.2x07.German.720p.HDTV", "series", {"season": 2, "episodes": [7]}),
    ("Tatort.Folge.12.German", "series", {"season": 1, "episodes": [12]}),
    ("Dune.Part.Two.2024.German.1080p.WEB-GRP", "movie", {"year": 2024}),
    ("Some.Movie.German.1080p.BluRay", "movie", {"year": None}),
    ("random_folder_name", "unknown", {}),
])
def test_classify(name, typ, extra):
    result = scanner.classify(name)
    assert result["type"] == typ
    for key, val in extra.items():
        assert result[key] == val


def test_classify_series_name():
    assert scanner.classify("The.Boys.S05E03.German.1080p")["series_name"] == "The Boys"


# ── find_releases: wrapper-in-wrapper ─────────────────────────────────────────

def test_plain_release_folder(tmp_path):
    make_files(tmp_path, "Movie.2022.German.1080p.WEB-GRP/movie.mkv")
    assert names(scanner.find_releases(tmp_path / "Movie.2022.German.1080p.WEB-GRP", EXTS)) == [
        "Movie.2022.German.1080p.WEB-GRP"]


def test_uuid_in_uuid(tmp_path):
    make_files(tmp_path, f"{UUID_A}/{UUID_B}/Movie.2022.German.1080p.WEB-GRP/m.mkv")
    assert names(scanner.find_releases(tmp_path / UUID_A, EXTS)) == ["Movie.2022.German.1080p.WEB-GRP"]


def test_uuid_then_hoster_wrapper(tmp_path):
    rel = "Show.S01.German.1080p.WEB-GRP"
    make_files(tmp_path, f"{UUID_A}/{rel} - rapidgator/{rel}/e01.mkv")
    found = scanner.find_releases(tmp_path / UUID_A, EXTS)
    assert found == [tmp_path / UUID_A / f"{rel} - rapidgator" / rel]


def test_release_name_then_uuid_then_release(tmp_path):
    rel = "Movie.2022.German.1080p.WEB-GRP"
    make_files(tmp_path, f"{rel}/{UUID_A}/{rel}/m.mkv")
    found = scanner.find_releases(tmp_path / rel, EXTS)
    # Must resolve to the innermost folder, not the outer one (which would put
    # the UUID folder onto the NAS)
    assert found == [tmp_path / rel / UUID_A / rel]


def test_wrapper_with_multiple_releases(tmp_path):
    make_files(tmp_path,
               f"{UUID_A}/Movie.A.2020.1080p.WEB-GRP/a.mkv",
               f"{UUID_A}/Movie.B.2021.1080p.WEB-GRP/b.mkv",
               f"{UUID_A}/_info/readme.txt")
    assert names(scanner.find_releases(tmp_path / UUID_A, EXTS)) == [
        "Movie.A.2020.1080p.WEB-GRP", "Movie.B.2021.1080p.WEB-GRP"]


def test_nested_wrapper_with_multiple_releases(tmp_path):
    make_files(tmp_path,
               f"{UUID_A}/{UUID_B}/Movie.A.2020.1080p.WEB-GRP/a.mkv",
               f"{UUID_A}/{UUID_B}/Show.S02E01.German.720p.WEB-GRP/e.mkv")
    assert names(scanner.find_releases(tmp_path / UUID_A, EXTS)) == [
        "Movie.A.2020.1080p.WEB-GRP", "Show.S02E01.German.720p.WEB-GRP"]


def test_generic_named_wrapper_with_multiple_releases(tmp_path):
    make_files(tmp_path,
               "my downloads pack/Movie.A.2020.1080p.WEB-GRP/a.mkv",
               "my downloads pack/Movie.B.2021.1080p.WEB-GRP/b.mkv")
    assert len(scanner.find_releases(tmp_path / "my downloads pack", EXTS)) == 2


def test_loose_file_in_wrapper(tmp_path):
    make_files(tmp_path, f"{UUID_A}/Movie.C.2019.1080p.WEB-GRP.mkv", f"{UUID_A}/info.nfo")
    assert scanner.find_releases(tmp_path / UUID_A, EXTS) == [
        tmp_path / UUID_A / "Movie.C.2019.1080p.WEB-GRP.mkv"]


def test_season_pack_with_episode_subfolders_is_not_split(tmp_path):
    pack = "Show.S01.German.1080p.WEB-GRP"
    make_files(tmp_path,
               f"{pack}/Show.S01E01.German.1080p.WEB-GRP/e1.mkv",
               f"{pack}/Show.S01E02.German.1080p.WEB-GRP/e2.mkv")
    assert names(scanner.find_releases(tmp_path / pack, EXTS)) == [pack]


def test_release_with_extras_is_kept_whole(tmp_path):
    rel = "Movie.2022.German.1080p.BluRay-GRP"
    make_files(tmp_path, f"{rel}/movie.mkv", f"{rel}/Sample/sample.mkv", f"{rel}/Subs/de.srt")
    assert names(scanner.find_releases(tmp_path / rel, EXTS)) == [rel]


def test_custom_wrapper_pattern(tmp_path):
    import re
    make_files(tmp_path, "jd-package-42/Movie.2022.German.1080p.WEB-GRP.mkv")
    found = scanner.find_releases(tmp_path / "jd-package-42", EXTS, [re.compile(r"^jd-package-\d+$")])
    assert names(found) == ["Movie.2022.German.1080p.WEB-GRP.mkv"]


def test_depth_limit_does_not_crash(tmp_path):
    deep = Path(*[f"{i:08x}-1111-2222-3333-444455556666" for i in range(10)])
    make_files(tmp_path, str(deep / "m.mkv"))
    assert len(scanner.find_releases(tmp_path / deep.parts[0], EXTS)) == 1


# ── scan_downloads ────────────────────────────────────────────────────────────

def test_scan_downloads_keeps_on_progress_parameter():
    # app.py calls scan_downloads(config, on_progress=...) — see CLAUDE.md
    assert "on_progress" in inspect.signature(scanner.scan_downloads).parameters


def test_scan_downloads_end_to_end(config, dirs):
    dl = dirs["download_dir"]
    make_files(dl,
               f"{UUID_A}/Movie.A.2020.German.1080p.WEB-GRP/a.mkv",
               f"{UUID_A}/Movie.B.2021.German.1080p.WEB-GRP/b.mkv",
               "The.Boys.S05E01.German.1080p.WEB-GRP.mkv",
               "Unfinished.2023.German.1080p.WEB-GRP/movie.mkv.part",
               "Unfinished.2023.German.1080p.WEB-GRP/movie.mkv",
               "notes.txt")
    (dirs["series_dir"] / "The Boys" / "S04").mkdir(parents=True)

    events = []
    items = scanner.scan_downloads(config, on_progress=events.append)
    by_name = {i["release_name"]: i for i in items}

    assert set(by_name) == {"Movie.A.2020.German.1080p.WEB-GRP", "Movie.B.2021.German.1080p.WEB-GRP",
                            "The.Boys.S05E01.German.1080p.WEB-GRP.mkv",
                            "Unfinished.2023.German.1080p.WEB-GRP"}

    a = by_name["Movie.A.2020.German.1080p.WEB-GRP"]
    assert a["is_wrapper"] and a["shared_wrapper"]
    assert a["source_top"] == str(dl / UUID_A)
    assert a["suggestion"]["dest_dir"] == str(dirs["movies_dir"] / "M" / "Movie.A.2020.German.1080p.WEB-GRP")

    ep = by_name["The.Boys.S05E01.German.1080p.WEB-GRP.mkv"]
    assert ep["is_loose_file"]
    assert ep["suggestion"]["series_name"] == "The Boys"
    assert ep["suggestion"]["match_auto"]
    assert ep["suggestion"]["video_files"] == ["The.Boys.S05E01.German.1080p.WEB-GRP.mkv"]
    assert ep["suggestion"]["existing_seasons"] == ["S04"]

    assert by_name["Unfinished.2023.German.1080p.WEB-GRP"]["incomplete"]
    assert not a["incomplete"]

    assert events[-1]["phase"] == "done"
    assert events[-1]["known_series"] == ["The Boys"]


def test_scan_missing_download_dir(config):
    config["download_dir"] = config["download_dir"] + "_missing"
    with pytest.raises(FileNotFoundError):
        scanner.scan_downloads(config)
