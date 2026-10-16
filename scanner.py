"""Scanner: walks the download directory and classifies entries."""

import logging
import os
import re
import unicodedata
from pathlib import Path

from config import PARTIAL_SUFFIX, video_extensions

log = logging.getLogger(__name__)

# ── Regexes ────────────────────────────────────────────────────────────────────

RE_SEASON_EP = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})", re.IGNORECASE)
RE_SEASON_ONLY = re.compile(r"(?:[._\- ]|(?<=\d))S(\d{1,2})(?![E\d])", re.IGNORECASE)
RE_SxE = re.compile(r"(?<!\d)(\d{1,2})x(\d{2})(?!\d)", re.IGNORECASE)
# Not inside a word ("Sleep.2023" is no "Ep 202") and not the start of a year
RE_FOLGE = re.compile(r"(?<![a-z])(?:Folge|Episode|Ep)[._\- ]?(\d{1,3})(?!\d)", re.IGNORECASE)
# Season sub-folders without the series name ("Season 1", "Staffel 02", "S03")
RE_SEASON_FOLDER = re.compile(r"^(?:(?:season|staffel)[._\- ]*\d{1,2}|s\d{1,2})$", re.IGNORECASE)
RE_YEAR = re.compile(r"\b(19|20)\d{2}\b")
RE_QUALITY = re.compile(
    r"\b(1080p|720p|2160p|4K|UHD|WEB|WEBRip|BluRay|BDRip|HDTV|NF|AMZN|DSNP|ATVP|PROPER|REPACK|EXTENDED)\b",
    re.IGNORECASE,
)
RE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
# Articles to preserve in display names but ignore when matching
# Hoster/site suffix added by download managers: "Release - serienfans.org".
# The space before the dash is required, otherwise "Spider-Man.No.Way.Home"
# would be cut down to "Spider".
RE_HOSTER_SUFFIX = re.compile(r"\s+-\s*\S+\.[a-z]{2,4}\s*$", re.IGNORECASE)
ARTICLES = re.compile(r"^(the|der|die|das|ein|eine|les|le|la|los|las)\s+", re.IGNORECASE)

# NAS/OS housekeeping entries that are never media (Synology @eaDir, #recycle, …)
IGNORED_NAMES = {"#recycle", "#snapshot", "lost+found", "$recycle.bin", "system volume information"}


def is_ignored(name: str) -> bool:
    return (name.startswith((".", "@")) or name.lower() in IGNORED_NAMES
            or name.endswith(PARTIAL_SUFFIX))   # unfinished copy of the mover


def _walk(path: Path):
    """os.walk that skips ignored housekeeping folders."""
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not is_ignored(d)]
        yield root, dirs, files


# ── NAS series library cache ───────────────────────────────────────────────────

class SeriesLibrary:
    """
    Reads the series directory once per scan and provides fuzzy matching
    so existing series folders are preferred over freshly-parsed names.
    """

    def __init__(self, series_dir: str):
        self.series_dir = Path(series_dir)
        # { normalized_key: actual_folder_name }
        self._index: dict[str, str] = {}
        self._match_cache: dict[str, tuple[str | None, float]] = {}
        self._load()

    def _load(self):
        self._index.clear()
        if not self.series_dir.exists():
            return
        for entry in self.series_dir.iterdir():
            if entry.is_dir() and not is_ignored(entry.name):
                self._index[_fuzzy_key(entry.name)] = entry.name
        log.info("SeriesLibrary: %d existing series indexed", len(self._index))

    def match(self, candidate: str) -> tuple[str | None, float]:
        """
        Try to find the best matching existing series folder for `candidate`.
        Returns (folder_name, score) where score is 0..1.
        Score >= 0.80 → use automatically; < 0.80 → suggest but flag as uncertain.
        """
        if not candidate:
            return None, 0.0
        key = _fuzzy_key(candidate)
        if not key:
            return None, 0.0
        # Episodes of the same series share a key → compare against the library once
        if key in self._match_cache:
            return self._match_cache[key]

        best_name = None
        best_score = 0.0

        for norm, actual in self._index.items():
            score = _token_similarity(key, norm)
            if score > best_score:
                best_score = score
                best_name = actual

        self._match_cache[key] = (best_name, best_score)
        return best_name, best_score

    def seasons_for(self, series_folder: str) -> list[str]:
        """Return existing season folder names (S01, S02 …) for a series."""
        p = self.series_dir / series_folder
        if not p.exists():
            return []
        return sorted(
            e.name for e in p.iterdir()
            if e.is_dir() and re.match(r"^S\d{2}$", e.name, re.IGNORECASE)
        )

    def all_names(self) -> list[str]:
        """Return all known series folder names, sorted."""
        return sorted(self._index.values())


def _fuzzy_key(name: str) -> str:
    """
    Normalise a series name for comparison:
    - strip articles (The/Der/Die/Das …)
    - lowercase
    - remove punctuation, separators, years, quality tags
    - remove common release suffixes
    """
    s = _ascii_fold(name)
    # Strip hoster/site suffixes like "- serienfans.org" or "- filecrypt.cc"
    s = RE_HOSTER_SUFFIX.sub("", s)
    # Strip articles
    s = ARTICLES.sub("", s)
    # Replace separators with space
    s = re.sub(r"[._\-]", " ", s)
    # Remove year
    s = RE_YEAR.sub("", s)
    # Remove everything from S01/S01E01/quality tag onward
    s = RE_SEASON_EP.split(s)[0]
    s = RE_SEASON_ONLY.split(s)[0]
    s = RE_QUALITY.split(s)[0]
    # Lowercase, keep only letters/digits
    s = re.sub(r"[^a-z0-9]", "", s.lower())
    return s.strip()


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def _ascii_fold(s: str) -> str:
    """'Die Brücke' → 'Die Bruecke', 'Pokémon' → 'Pokemon' (release names are ASCII)."""
    s = unicodedata.normalize("NFC", s).translate(_UMLAUTS)
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _token_similarity(a: str, b: str) -> float:
    """
    Character-level similarity between two already-normalised strings.
    Uses longest common subsequence ratio as a simple, dependency-free metric.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    # Quick prefix/contains bonus
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if longer.startswith(shorter):
        return len(shorter) / len(longer)

    # LCS
    lcs = _lcs_len(a, b)
    return (2 * lcs) / (len(a) + len(b))


def _lcs_len(a: str, b: str) -> int:
    """Length of longest common subsequence (capped at 60 chars for speed)."""
    a, b = a[:60], b[:60]
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    for i in range(m):
        curr = [0] * (n + 1)
        for j in range(n):
            if a[i] == b[j]:
                curr[j + 1] = prev[j] + 1
            else:
                curr[j + 1] = max(curr[j], prev[j + 1])
        prev = curr
    return prev[n]


# ── Wrapper detection ─────────────────────────────────────────────────────────

MAX_WRAPPER_DEPTH = 6

# Sub-folders that belong to a release and never count as a nested release
EXTRA_DIRS = {"sample", "samples", "subs", "subtitles", "proof", "extras",
              "featurettes", "screens", "screenshots", "covers", "artwork"}

# Sample clips ("release-sample.mkv", "sample.mkv", "Sample/…") are never episodes
RE_SAMPLE = re.compile(r"(?:^|[._\- ])sample(?:[._\- ]|$)", re.IGNORECASE)
SAMPLE_DIRS = {"sample", "samples"}

# Suffixes of files that are still being downloaded/extracted
PARTIAL_SUFFIXES = {".part", ".crdownload", ".partial", ".download", ".tmp", ".!qb", PARTIAL_SUFFIX}


def is_strong_wrapper(name: str, extra_patterns: list[re.Pattern] | tuple = ()) -> bool:
    """Name is certainly not a release (UUID or a configured wrapper pattern)."""
    if RE_UUID.match(name):
        return True
    return any(p.search(name) for p in extra_patterns)


def is_uuid_wrapper(name: str, extra_patterns: list[re.Pattern] | tuple = ()) -> bool:
    """Strong wrapper, or a short generic name without any quality tag."""
    if is_strong_wrapper(name, extra_patterns):
        return True
    if len(name) < 10 and not RE_QUALITY.search(name):
        return True
    return False


def _normalize(name: str) -> str:
    return re.sub(r"[\W_]+", "", name.lower())


def is_name_wrapper(parent_name: str, child_name: str) -> bool:
    pn = _normalize(parent_name)
    cn = _normalize(child_name)
    if pn == cn:
        return True
    if cn and pn.startswith(cn):
        return True
    if pn and cn.startswith(pn):
        return True
    if RE_QUALITY.search(child_name) and not RE_QUALITY.search(parent_name):
        if RE_YEAR.search(child_name) or RE_SEASON_EP.search(child_name):
            return True
    return False


def _contains_video(path: Path, exts: set[str]) -> bool:
    for _, _, files in _walk(path):
        if any(Path(f).suffix.lower() in exts for f in files):
            return True
    return False


def find_releases(
    path: Path,
    exts: set[str],
    extra_patterns: list[re.Pattern] | tuple = (),
    depth: int = 0,
) -> list[Path]:
    """
    Resolve a top-level download entry to the real release(s) inside it.

    Handles arbitrarily nested wrappers (UUID › hoster-suffix › release,
    release-name › UUID › release, …) and wrappers holding several releases or
    loose video files. Each returned path is a release folder or a single video
    file. Returns an empty list only for plain non-video files.
    """
    if path.is_file():
        return [path] if path.suffix.lower() in exts else []
    if depth > MAX_WRAPPER_DEPTH:
        return [path]

    try:
        children = sorted(c for c in path.iterdir() if not is_ignored(c.name))
    except OSError:
        return [path]

    name = path.name
    strong = is_strong_wrapper(name, extra_patterns)
    videos = [c for c in children if c.is_file() and c.suffix.lower() in exts]
    subdirs = [c for c in children if c.is_dir() and c.name.lower() not in EXTRA_DIRS]

    # Video files directly inside: a regular folder is the release itself
    # (movie folder, season pack). A UUID-like wrapper only holds loose files.
    if videos and not strong:
        return [path]

    releases: list[Path] = list(videos)

    if len(subdirs) == 1 and not videos:
        child = subdirs[0]
        if (is_uuid_wrapper(name, extra_patterns)
                or is_strong_wrapper(child.name, extra_patterns)
                or is_name_wrapper(name, child.name)):
            return find_releases(child, exts, extra_patterns, depth + 1)
        return [path]

    if len(subdirs) > 1 or (subdirs and videos):
        candidates = [c for c in subdirs if _contains_video(c, exts)]
        split = is_uuid_wrapper(name, extra_patterns) or (
            classify(name)["type"] == "unknown"
            and sum(classify(c.name)["type"] != "unknown" or bool(RE_SEASON_FOLDER.match(c.name))
                    for c in candidates) >= 2
        )
        if split:
            for child in candidates:
                releases.extend(find_releases(child, exts, extra_patterns, depth + 1))
        elif not videos:
            return [path]

    return releases or [path]


# ── Classification ────────────────────────────────────────────────────────────

def classify(release_name: str) -> dict:
    result = {
        "type": "unknown",
        "series_name": None,
        "season": None,
        "episodes": [],
        "title": None,
        "year": None,
        "confidence": "low",
        "is_season_pack": False,
    }

    # S01E01
    m = RE_SEASON_EP.search(release_name)
    if m:
        result.update(type="series", season=int(m.group(1)),
                      episodes=[int(m.group(2))], confidence="high")
        before = release_name[: m.start()].strip("._- ")
        result["series_name"] = _clean_name(before, keep_article=True)
        return result

    # S01 season pack
    m = RE_SEASON_ONLY.search(release_name)
    if m:
        result.update(type="series", season=int(m.group(1)),
                      episodes=[], confidence="high", is_season_pack=True)
        before = release_name[: m.start()].strip("._- ")
        result["series_name"] = _clean_name(before, keep_article=True) or release_name
        return result

    # 1x01
    m = RE_SxE.search(release_name)
    if m:
        result.update(type="series", season=int(m.group(1)),
                      episodes=[int(m.group(2))], confidence="high")
        before = release_name[: m.start()].strip("._- ")
        result["series_name"] = _clean_name(before, keep_article=True)
        return result

    # Folge/Episode/Ep
    m = RE_FOLGE.search(release_name)
    if m:
        result.update(type="series", season=1,
                      episodes=[int(m.group(1))], confidence="medium")
        before = release_name[: m.start()].strip("._- ")
        result["series_name"] = _clean_name(before, keep_article=True)
        return result

    # Movie: year + quality
    year_m = RE_YEAR.search(release_name)
    quality_m = RE_QUALITY.search(release_name)
    if year_m and quality_m:
        result.update(type="movie", year=int(year_m.group(0)), confidence="high")
        before = release_name[: year_m.start()].strip("._- ")
        result["title"] = _clean_name(before, keep_article=True)
        return result

    # Movie: quality only
    if quality_m:
        result.update(type="movie", confidence="medium")
        before = release_name[: quality_m.start()].strip("._- ")
        result["title"] = _clean_name(before, keep_article=True) or release_name
        return result

    # Unknown
    result["title"] = _clean_name(release_name, keep_article=True) or release_name
    return result


def _clean_name(raw: str, keep_article: bool = True) -> str:
    """Dots/underscores → spaces, strip hoster suffixes, optionally keep articles."""
    # Strip hoster suffix: "- serienfans.org" style
    s = RE_HOSTER_SUFFIX.sub("", raw)
    s = re.sub(r"[._]", " ", s).strip()
    # Remove trailing year
    s = re.sub(r"\s+\d{4}$", "", s).strip()
    return s


# ── Destination suggestion ────────────────────────────────────────────────────

# Only an identical normalised name is used automatically: similar names are
# often different shows ("Reacher" vs "Preacher" = 0.93, "Chucky" vs "Chuck")
MATCH_AUTO = 1.0
MATCH_HINT = 0.50   # score above this → suggest but mark uncertain
DUPLICATE_THRESHOLD = 0.90  # movie titles must be (almost) identical to count as duplicate


def suggest_destination(
    release_folder: Path,
    classification: dict,
    config: dict,
    library: "SeriesLibrary | None" = None,
    movie_index: "MovieIndex | None" = None,
) -> dict:
    rtype = classification["type"]
    base_name = release_folder.stem if release_folder.is_file() else release_folder.name

    if rtype == "series":
        raw_name  = classification["series_name"] or base_name
        season    = classification["season"] or 1
        season_str = f"S{season:02d}"

        # ── Match against existing NAS library ────────────────────────────────
        matched_name  = None
        match_score   = 0.0
        match_auto    = False
        existing_seasons: list[str] = []

        if library:
            matched_name, match_score = library.match(raw_name)
            if match_score < MATCH_HINT:
                matched_name = None          # too weak to even suggest
            match_auto = match_score >= MATCH_AUTO
            if matched_name:
                existing_seasons = library.seasons_for(matched_name)

        # Decide which name to use for the path
        if match_auto and matched_name:
            series_name = matched_name
        else:
            series_name = raw_name   # parsed from release, user can edit

        dest_dir = os.path.join(config["series_dir"], series_name, season_str)
        season_exists = season_str in existing_seasons

        video_files = find_videos(release_folder, config)
        video_base = release_folder.parent if release_folder.is_file() else release_folder

        return {
            "type": "series",
            "series_name": series_name,
            "season": season,
            "dest_dir": dest_dir,
            "is_season_pack": classification.get("is_season_pack", False),
            "video_files": [v.relative_to(video_base).as_posix() for v in video_files],
            "confidence": classification["confidence"],
            # Library match info for the UI
            "matched_existing": matched_name if matched_name else None,
            "match_score": round(match_score, 2),
            "match_auto": match_auto,
            "existing_seasons": existing_seasons,
            "season_exists": season_exists,
        }

    elif rtype == "movie":
        folder_name = release_folder.name
        first_char  = _first_letter(classification.get("title") or folder_name)
        dest_base   = os.path.join(config["movies_dir"], first_char, folder_name)
        title       = classification.get("title") or folder_name
        year        = classification.get("year")

        # Check for an existing release of the same title+year anywhere in movies_dir
        if movie_index is None:
            movie_index = MovieIndex(config["movies_dir"])
        duplicate_path = movie_index.find_duplicate(folder_name, year)

        return {
            "type": "movie",
            "title": title,
            "year": year,
            "first_char": first_char,
            "dest_dir": dest_base,
            "confidence": classification["confidence"],
            "duplicate_path": duplicate_path,   # existing NAS path or None
        }

    else:
        folder_name = release_folder.name
        # Same letter rule as for movies (and the browser): parsed title, articles ignored
        first_char  = _first_letter(classification.get("title") or folder_name)
        return {
            "type": "unknown",
            "title": classification.get("title") or folder_name,
            "first_char": first_char,
            "dest_dir": os.path.join(config["movies_dir"], first_char, folder_name),
            "confidence": "low",
        }


def _first_letter(name: str) -> str:
    clean = ARTICLES.sub("", name)
    letter = clean[0].upper() if clean else "#"
    return letter if letter.isalpha() else "#"


RE_ANY_YEAR = re.compile(r"[\s._\-]*[(\[]?\b((?:19|20)\d{2})\b[)\]]?")


def _title_key(name: str) -> tuple[str, int | None]:
    """
    (normalised title, year) of a movie release or library folder for
    duplicate detection. Understands release names ("Dune.2021.German…") and
    media-server folders ("Dune (2021)", "Dune [2021]", "Dune 2021").

    Only the title is compared — language/codec/source tags like
    "German.EAC3.DL" are shared by most releases and would otherwise dominate
    the similarity (Exit.8.2025… vs Elio.2025… scored 88 %).
    """
    c = classify(name)
    title, year = c.get("title") or "", c.get("year")
    if year is None:
        m = RE_ANY_YEAR.search(name)
        if m and m.start() > 0:
            title, year = _clean_name(name[:m.start()]), int(m.group(1))
    title = ARTICLES.sub("", _ascii_fold(title))
    return re.sub(r"[^a-z0-9]", "", title.lower()), year


class MovieIndex:
    """
    Lists movies_dir/<Letter>/<Release> once per scan (lazily, on the first
    movie) instead of walking the whole NAS movie tree for every movie found.
    Movie folders directly in movies_dir (flat libraries) are indexed too.
    """

    def __init__(self, movies_dir: str):
        self.movies_dir = Path(movies_dir)
        # (name, path, (title_key, year))
        self._entries: list[tuple[str, str, tuple[str, int | None]]] | None = None

    def _load(self) -> list[tuple[str, str, tuple[str, int | None]]]:
        entries = []
        if not self.movies_dir.exists():
            return entries
        try:
            for letter_dir in sorted(self.movies_dir.iterdir()):
                if not letter_dir.is_dir() or is_ignored(letter_dir.name):
                    continue
                if len(letter_dir.name) > 2:
                    # not a letter folder → a movie of a flat library ("Dune (2021)")
                    entries.append((letter_dir.name, str(letter_dir), _title_key(letter_dir.name)))
                    continue
                for entry in letter_dir.iterdir():
                    if entry.is_dir() and not is_ignored(entry.name):
                        entries.append((entry.name, str(entry), _title_key(entry.name)))
        except OSError as e:
            log.warning("Could not index %s: %s", self.movies_dir, e)
        log.info("MovieIndex: %d existing movies indexed", len(entries))
        return entries

    def find_duplicate(self, new_folder: str, year: int | None) -> str | None:
        """
        Existing folder with the same title+year but a different release name
        (e.g. another group). Searches all letter folders to catch releases that
        were filed under a different letter (The → T vs M).

        Both sides need a year: "Replace" deletes the old folder, and without
        years a remake ("Halloween.2018") would match the original ("Halloween").
        """
        new_key, new_year = _title_key(new_folder)
        year = year or new_year
        if not new_key:
            return None
        if self._entries is None:
            self._entries = self._load()
        for name, path, (existing_key, existing_year) in self._entries:
            if name == new_folder or not existing_key:
                continue  # exact same name → handled by dest_exists
            if not (year and existing_year) or year != existing_year:
                continue
            if _token_similarity(new_key, existing_key) >= DUPLICATE_THRESHOLD:
                return path
        return None


def is_sample(path: Path) -> bool:
    return bool(RE_SAMPLE.search(path.stem))


def find_videos(folder: Path, config: dict) -> list[Path]:
    """Video files of a release — without samples and without Sample/ folders."""
    if folder.is_file():
        return [folder]
    exts = video_extensions(config)
    videos = []
    for root, dirs, files in _walk(folder):
        dirs[:] = [d for d in dirs if d.lower() not in SAMPLE_DIRS]
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in exts and not is_sample(p):
                videos.append(p)
    return sorted(videos)


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan_downloads(config: dict, on_progress=None) -> list[dict]:
    dl_dir = Path(config["download_dir"])
    if not dl_dir.exists():
        raise FileNotFoundError(f"Download directory not found: {dl_dir}")

    def emit(phase: str, current: str = "", found: int = 0, current_key: str | None = None,
             current_args: dict | None = None, **extra):
        # `current` is a file/folder name; status messages are sent as i18n
        # keys (`current_key` + `current_args`) and translated in the browser
        if on_progress:
            on_progress({"phase": phase, "current": current, "found": found,
                         "current_key": current_key, "current_args": current_args or {}, **extra})

    emit("init", current_key="scan.reading_downloads")

    all_entries = sorted(e for e in dl_dir.iterdir() if not is_ignored(e.name))

    emit("library", current_key="scan.loading_library")
    library = SeriesLibrary(config["series_dir"])
    movie_index = MovieIndex(config["movies_dir"])
    emit("library", current_key="scan.library_indexed", current_args={"count": len(library.all_names())})

    exts = video_extensions(config)
    wrapper_res = [re.compile(p, re.IGNORECASE) for p in config.get("wrapper_patterns") or []]

    items = []
    seen_releases: set[str] = set()

    for entry in all_entries:
        emit("scanning", entry.name, len(items))
        try:
            releases = find_releases(entry, exts, wrapper_res)
            for release in releases:
                release_key = str(release.resolve())
                if release_key in seen_releases:
                    continue
                seen_releases.add(release_key)
                shared = len(releases) > 1 or _wrapper_has_other_content(entry, release)
                items.append(_process_release(entry, release, config, library, movie_index,
                                              shared_wrapper=shared))
        except Exception as e:
            log.warning("Error processing %s: %s", entry, e)

    emit("done", found=len(items), current_key="scan.found", current_args={"count": len(items)},
         known_series=library.all_names())
    return items


def _classify_by_videos(top_entry: Path, folder: Path, config: dict) -> dict | None:
    """
    Classification of a folder from its video file names — if they all are
    episodes of one season of one series. Otherwise None (the folder stays
    "unknown": several seasons in one folder can't go into one season folder).
    """
    videos = find_videos(folder, config)
    if not videos:
        return None
    found = [classify(v.stem) for v in videos]
    if any(c["type"] != "series" for c in found):
        return None
    if len({c["season"] for c in found}) != 1 or len({_fuzzy_key(c["series_name"] or "") for c in found}) != 1:
        return None
    result = dict(found[0])
    if not result["series_name"]:
        # "S01E01.mkv": the folder carries the name ("Dark/Season 1/" → "Dark")
        named = folder.parent if RE_SEASON_FOLDER.match(folder.name) and folder != top_entry else folder
        result["series_name"] = _clean_name(named.name)
    result.update(episodes=sorted({e for c in found for e in c["episodes"]}),
                  is_season_pack=len(videos) > 1, confidence="medium")
    return result


def _wrapper_has_other_content(top: Path, release: Path) -> bool:
    """
    True if a wrapper holds anything besides the path down to `release` — e.g.
    a second download that is still running (.part only, so not a release yet).
    Then "delete" must only remove the release, never the whole wrapper.
    """
    if top == release:
        return False
    current = top
    for part in release.relative_to(top).parts:
        try:
            if any(c.name != part and not is_ignored(c.name) for c in current.iterdir()):
                return True
        except OSError:
            return True   # can't tell → be careful
        current = current / part
    return False


def _process_release(
    top_entry: Path, release: Path, config: dict, library: SeriesLibrary,
    movie_index: MovieIndex | None = None, shared_wrapper: bool = False,
) -> dict:
    is_file = release.is_file()
    classification = classify(release.stem if is_file else release.name)
    if classification["type"] == "unknown" and not is_file:
        # "Breaking Bad/", "Dark/Season 1/": the folder name says nothing, the episodes do
        classification = _classify_by_videos(top_entry, release, config) or classification
    suggestion     = suggest_destination(release, classification, config, library, movie_index)

    if is_file and suggestion["type"] != "series":
        # Loose movie file: goes directly into the letter folder
        suggestion["dest_dir"] = os.path.join(
            config["movies_dir"], suggestion.get("first_char", "#"), release.name,
        )

    size, incomplete = _dir_stats(release, config)
    is_wrapper = top_entry != release
    return {
        "id":             str(release.resolve()),
        "source_top":     str(top_entry),
        "source_release": str(release),
        "release_name":   release.name,
        "is_wrapper":     is_wrapper,
        "wrapper_name":   top_entry.name if is_wrapper else None,
        # Path from the top-level entry down to the release, e.g. "uuid/Foo - hoster/Foo"
        "wrapper_path":   release.relative_to(top_entry).as_posix() if is_wrapper else None,
        # Several releases live in the same wrapper → only delete the release itself
        "shared_wrapper": shared_wrapper,
        "suggestion":     suggestion,
        "dest_exists":    os.path.exists(suggestion["dest_dir"]),
        "size_bytes":     size,
        "incomplete":     incomplete,
        "is_loose_file":  is_file,
    }


def _dir_stats(path: Path, config: dict | None = None) -> tuple[int, bool]:
    """
    Approximate the size of a release for display purposes and detect files
    that are still being downloaded (``.part``, ``.crdownload``, …).

    Only stats video files instead of every file in the tree. On SMB/NFS each
    stat() is a network round-trip; release folders often carry extras (.nfo,
    .srt, sample.mkv, screenshots, ...) that add many extra round-trips while
    contributing nothing useful to a "how big is this" estimate. If no video
    files are found, falls back to a top-level-only estimate instead of
    walking everything.
    """
    if path.is_file():
        return path.stat().st_size, False

    exts = video_extensions(config or {})
    total = 0
    found_any = False
    incomplete = False
    for root, _, files in _walk(path):
        for f in files:
            suffix = Path(f).suffix.lower()
            if suffix in PARTIAL_SUFFIXES:
                incomplete = True
            if suffix not in exts:
                continue
            try:
                total += (Path(root) / f).stat().st_size
                found_any = True
            except OSError:
                pass

    if found_any:
        return total, incomplete

    # No video files found (e.g. wrong extensions configured) — fall back to
    # a top-level-only estimate instead of a full recursive stat of everything.
    try:
        for entry in path.iterdir():
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        pass
    return total, incomplete
