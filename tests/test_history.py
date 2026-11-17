"""history.jsonl: retention, server-side search/filter/paging."""

import pytest
from test_app import auth_client, client  # noqa: F401  (fixtures)

import history


@pytest.fixture
def hist_file(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    monkeypatch.setattr(history, "HISTORY_FILE", path)
    return path


def _add(name: str, type_: str = "movie", ok: bool = True, dest: str = "/mnt/Filme", **kw):
    history.append([{"source_release": f"/dl/{name}", "type": type_, "dest_dir": f"{dest}/{name}"}],
                   [{"ok": ok, "error": None if ok else "boom"}], **kw)


def _add_many(names: list[str]):
    history.append([{"source_release": f"/dl/{n}", "type": "movie", "dest_dir": f"/mnt/Filme/{n}"} for n in names],
                   [{"ok": True}] * len(names))


def test_keeps_more_than_500_by_default(hist_file):
    _add_many([f"Movie.{i}" for i in range(600)])
    assert history.count() == 600


def test_trims_to_max_entries_and_keeps_newest(hist_file):
    for i in range(5):
        _add(f"Movie.{i}", max_entries=3)
    assert [e["release_name"] for e in history.read(10)] == ["Movie.4", "Movie.3", "Movie.2"]
    assert not hist_file.with_name("history.jsonl.tmp").exists()


def test_zero_means_unlimited(hist_file):
    for i in range(5):
        _add(f"Movie.{i}", max_entries=0)
    assert history.count() == 5


def test_query_search_filter_and_paging(hist_file):
    _add("The.Boys.S05E01", "series", dest="/mnt/Serien")
    _add("Dune.Part.Two.2024", "movie")
    _add("The.Boys.S05E02", "series", ok=False, dest="/mnt/Serien")
    _add("Oppenheimer.2023", "movie")

    names = lambda entries: [e["release_name"] for e in entries]   # noqa: E731
    assert names(history.query("boys")[0]) == ["The.Boys.S05E02", "The.Boys.S05E01"]
    assert names(history.query("BOYS e01")[0]) == ["The.Boys.S05E01"]           # all words, any case
    assert names(history.query("serien")[0]) == ["The.Boys.S05E02", "The.Boys.S05E01"]   # destination too
    assert names(history.query(type_filter="movie")[0]) == ["Oppenheimer.2023", "Dune.Part.Two.2024"]
    assert names(history.query(type_filter="error")[0]) == ["The.Boys.S05E02"]

    page, matched = history.query(offset=1, limit=2)
    assert names(page) == ["The.Boys.S05E02", "Dune.Part.Two.2024"] and matched == 4


def test_broken_lines_are_skipped(hist_file):
    _add("Good.Movie")
    with open(hist_file, "a", encoding="utf-8") as f:
        f.write("{not json\n\n[1, 2]\n")
    assert [e["release_name"] for e in history.read()] == ["Good.Movie"]


def test_api_searches_the_whole_history(auth_client, hist_file):  # noqa: F811
    _add("Old.Needle.2001")
    _add_many([f"Filler.{i}" for i in range(300)])

    data = auth_client.get("/api/history?q=needle").get_json()
    assert [e["release_name"] for e in data["entries"]] == ["Old.Needle.2001"]
    assert data["matched"] == 1 and data["total"] == 301

    page2 = auth_client.get("/api/history?limit=100&offset=100").get_json()
    assert len(page2["entries"]) == 100 and page2["matched"] == 301
    assert page2["entries"][0]["release_name"] == "Filler.199"


def test_api_ignores_bogus_params(auth_client, hist_file):  # noqa: F811
    _add("A.Movie")
    data = auth_client.get("/api/history?filter=nope&offset=-5&limit=abc").get_json()
    assert data["ok"] and data["matched"] == 1 and data["offset"] == 0
