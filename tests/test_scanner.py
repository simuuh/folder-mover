import inspect
import os
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


# ── Audit fixes ───────────────────────────────────────────────────────────────

def test_nas_housekeeping_folders_are_ignored(config, dirs):
    make_files(dirs["download_dir"],
               "@eaDir/thumb.jpg", "#recycle/Old.Movie.2001.1080p.WEB-GRP/m.mkv", ".Trash-1000/x.mkv",
               f"{UUID_A}/Movie.A.2020.1080p.WEB-GRP/a.mkv", f"{UUID_A}/@eaDir/a.mkv/SYNO_THUMB.jpg")
    (dirs["series_dir"] / "@eaDir").mkdir()
    items = scanner.scan_downloads(config)
    assert [i["release_name"] for i in items] == ["Movie.A.2020.1080p.WEB-GRP"]
    assert scanner.SeriesLibrary(config["series_dir"]).all_names() == []


def test_weak_library_match_is_not_suggested(config, dirs):
    (dirs["series_dir"] / "Completely Different Show").mkdir()
    library = scanner.SeriesLibrary(config["series_dir"])
    release = dirs["download_dir"] / "Xyz.S01E01.German.1080p.WEB-GRP.mkv"
    make_files(dirs["download_dir"], release.name)
    s = scanner.suggest_destination(release, scanner.classify(release.stem), config, library)
    assert s["matched_existing"] is None and not s["match_auto"]


def test_loose_episode_without_name_uses_stem(config, dirs):
    make_files(dirs["download_dir"], "S01E01.mkv")
    item = scanner.scan_downloads(config)[0]
    assert not item["suggestion"]["series_name"].endswith(".mkv")


def test_movie_duplicate_found_via_index(config, dirs):
    (dirs["movies_dir"] / "D" / "Dune.2021.German.720p.BluRay-OLD").mkdir(parents=True)
    make_files(dirs["download_dir"], "Dune.2021.German.1080p.WEB-NEW/dune.mkv")
    item = scanner.scan_downloads(config)[0]
    assert item["suggestion"]["duplicate_path"].endswith("Dune.2021.German.720p.BluRay-OLD")


@pytest.mark.parametrize("existing, new, expected", [
    # different title, same year + same tags → NOT a duplicate (real-world false positive)
    ("Elio.2025.German.EAC3.DL.1080p.BluRay.x265-VECTOR", "Exit.8.2025.German.EAC3.DL.1080p.BluRay.x264-NEXUS", False),
    # same title + year, different release → duplicate
    ("Dune.2021.German.720p.BluRay-OLD", "Dune.2021.German.DL.2160p.UHD.BluRay-NEW", True),
    # same title, different year (remake) → not a duplicate
    ("Dune.1984.German.1080p.BluRay-OLD", "Dune.2021.German.1080p.WEB-NEW", False),
    # article and punctuation differences don't matter
    ("Spider-Man.No.Way.Home.2021.German.1080p.WEB-A", "The.Spiderman.No.Way.Home.2021.German.720p.WEB-B", True),
])
def test_movie_duplicate_detection(tmp_path, existing, new, expected):
    letter = (tmp_path / "X")
    (letter / existing).mkdir(parents=True)
    found = scanner.MovieIndex(str(tmp_path)).find_duplicate(new, scanner.classify(new)["year"])
    assert (found is not None) == expected


@pytest.mark.parametrize("name, title", [
    ("Spider-Man.No.Way.Home.2021.German.1080p.WEB-GRP", "Spider-Man No Way Home"),
    ("X-Men.2000.German.1080p.BluRay-GRP", "X-Men"),
    ("Some Movie 2020 - serienfans.org", "Some Movie"),
])
def test_hyphenated_titles_and_hoster_suffix(name, title):
    assert scanner.classify(name)["title"] == title


def test_unfinished_mover_copies_are_not_indexed(config, dirs):
    (dirs["movies_dir"] / "D" / "Dune.2021.German.720p.BluRay-OLD.fmpart").mkdir(parents=True)
    (dirs["series_dir"] / "Show.fmpart").mkdir()
    assert scanner.MovieIndex(config["movies_dir"]).find_duplicate("Dune.2021.German.1080p.WEB-NEW", 2021) is None
    assert scanner.SeriesLibrary(config["series_dir"]).all_names() == []


def test_wrapper_with_unfinished_sibling_is_shared(config, dirs):
    # Movie.B is still downloading (.part only) → not a release, but deleting
    # Movie.A must not take the whole wrapper with it
    make_files(dirs["download_dir"], f"{UUID_A}/Movie.A.2020.German.1080p.WEB-GRP/a.mkv",
               f"{UUID_A}/Movie.B.2021.German.1080p.WEB-GRP/b.mkv.part")
    items = scanner.scan_downloads(config)
    assert [i["release_name"] for i in items] == ["Movie.A.2020.German.1080p.WEB-GRP"]
    assert items[0]["shared_wrapper"] is True


def test_wrapper_with_only_the_release_is_not_shared(config, dirs):
    make_files(dirs["download_dir"], f"{UUID_A}/Movie.A.2020.German.1080p.WEB-GRP - hoster/"
                                     "Movie.A.2020.German.1080p.WEB-GRP/a.mkv", f"{UUID_A}/@eaDir/x")
    item = scanner.scan_downloads(config)[0]
    assert item["is_wrapper"] and item["shared_wrapper"] is False


@pytest.mark.parametrize("name", ["Sleep.2023.German.1080p.WEB.x264-GRP", "Creep.2014.German.1080p.BluRay.x264-GRP",
                                  "Deep.2021.German.1080p.WEB.h264-GRP"])
def test_title_ending_in_ep_is_no_episode(name):
    assert scanner.classify(name)["type"] == "movie"


@pytest.mark.parametrize("name, episode", [("Tatort.Folge.12.German", 12), ("Show.Episode.3.German", 3),
                                           ("Show.Ep07.German", 7)])
def test_folge_episode_still_recognised(name, episode):
    assert scanner.classify(name)["episodes"] == [episode]


@pytest.mark.parametrize("library, release", [("Preacher", "Reacher.S02E01.German.1080p.WEB.h264-GRP"),
                                              ("Chuck", "Chucky.S01E01.German.1080p.WEB.h264-GRP")])
def test_similar_series_name_is_only_a_hint(config, dirs, library, release):
    (dirs["series_dir"] / library).mkdir()
    make_files(dirs["download_dir"], f"{release}/{release}.mkv")
    s = scanner.scan_downloads(config)[0]["suggestion"]
    assert not s["match_auto"] and s["matched_existing"] == library     # shown as "~ Preacher"
    assert s["series_name"] != library and library not in s["dest_dir"].split(os.sep)


@pytest.mark.parametrize("library, release", [("Pokémon", "Pokemon.S01E01.German.WEB"),
                                              ("Die Brücke", "Die.Bruecke.S02E01.German.WEB"),
                                              ("The Boys (2019)", "The.Boys.S05E01.German.WEB")])
def test_identical_series_name_is_matched_automatically(config, dirs, library, release):
    (dirs["series_dir"] / library).mkdir()
    make_files(dirs["download_dir"], f"{release}.mkv")
    s = scanner.scan_downloads(config)[0]["suggestion"]
    assert s["match_auto"] and s["series_name"] == library


@pytest.mark.parametrize("existing, new, expected", [
    ("I/Inception (2010)", "Inception.2010.German.DL.1080p.BluRay.x264-GRP", True),      # media-server naming
    ("Inception [2010]", "Inception.2010.German.DL.1080p.BluRay.x264-GRP", True),       # flat library
    ("H/Halloween", "Halloween.2018.German.DL.1080p.BluRay.x264-GRP", False),           # no year → no "replace"
    ("H/Halloween (1978)", "Halloween.2018.German.DL.1080p.BluRay.x264-GRP", False),
])
def test_duplicates_in_media_server_libraries(tmp_path, existing, new, expected):
    (tmp_path / existing).mkdir(parents=True)
    found = scanner.MovieIndex(str(tmp_path)).find_duplicate(new, scanner.classify(new)["year"])
    assert (found is not None) == expected


def test_series_folder_without_episode_tag_is_classified_by_its_episodes(config, dirs):
    make_files(dirs["download_dir"], "Breaking Bad/Breaking.Bad.S01E01.mkv", "Breaking Bad/Breaking.Bad.S01E02.mkv",
               "Dark/Season 1/Dark.S01E01.mkv", "Dark/Season 2/Dark.S02E01.mkv",
               "Babylon Berlin/Staffel 1/S01E01.mkv", "Babylon Berlin/Staffel 2/S02E01.mkv",
               "Mixed/Show.S01E01.mkv", "Mixed/Show.S02E01.mkv")
    by_dest = sorted(os.path.relpath(i["suggestion"]["dest_dir"], dirs["download_dir"].parent)
                     for i in scanner.scan_downloads(config))
    assert by_dest == sorted([
        os.path.join("Serien", "Babylon Berlin", "S01"), os.path.join("Serien", "Babylon Berlin", "S02"),
        os.path.join("Serien", "Breaking Bad", "S01"),
        os.path.join("Serien", "Dark", "S01"), os.path.join("Serien", "Dark", "S02"),
        os.path.join("Filme", "M", "Mixed"),             # two seasons in one folder → stays "unknown"
    ])


def test_letter_folder_ignores_articles_for_movies_and_unknown(config, dirs):
    make_files(dirs["download_dir"], "The.Sheriff.2026.German.1080p.WEB-SiXTYNiNE/a.mkv",
               "The.Sheriff.Directors.Cut/b.mkv")
    letters = {Path(i["suggestion"]["dest_dir"]).parent.name for i in scanner.scan_downloads(config)}
    assert letters == {"S"}
