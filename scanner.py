"""Scanner: walks the download directory and classifies entries."""

import os
import re
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ── Regexes ────────────────────────────────────────────────────────────────────

RE_SEASON_EP = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})", re.IGNORECASE)
RE_SEASON_ONLY = re.compile(r"(?:[._\- ]|(?<=\d))S(\d{1,2})(?![E\d])", re.IGNORECASE)
RE_SxE = re.compile(r"(?<!\d)(\d{1,2})x(\d{2})(?!\d)", re.IGNORECASE)
RE_FOLGE = re.compile(r"(?:Folge|Episode|Ep)[._\- ]?(\d{1,3})", re.IGNORECASE)
RE_YEAR = re.compile(r"\b(19|20)\d{2}\b")
RE_QUALITY = re.compile(
    r"\b(1080p|720p|2160p|4K|UHD|WEB|WEBRip|BluRay|BDRip|HDTV|NF|AMZN|DSNP|ATVP|PROPER|REPACK|EXTENDED)\b",
    re.IGNORECASE,
)
RE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
# Articles to preserve in display names but ignore when matching
ARTICLES = re.compile(r"^(the|der|die|das|ein|eine|les|le|la|los|las)\s+", re.IGNORECASE)


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
        self._load()

    def _load(self):
        self._index.clear()
        if not self.series_dir.exists():
            return
        for entry in self.series_dir.iterdir():
            if entry.is_dir():
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

        best_name = None
        best_score = 0.0

        for norm, actual in self._index.items():
            score = _token_similarity(key, norm)
            if score > best_score:
                best_score = score
                best_name = actual

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
    s = name
    # Strip hoster/site suffixes like "- serienfans.org" or "- filecrypt.cc"
    s = re.sub(r"\s*-\s*\S+\.\w{2,4}\s*$", "", s)
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

def is_uuid_wrapper(name: str) -> bool:
    if RE_UUID.match(name):
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


def find_release_folder(path: Path, depth: int = 0) -> Path | None:
    if depth > 4:
        return None
    name = path.name
    if is_uuid_wrapper(name):
        try:
            children = [c for c in path.iterdir() if c.is_dir()]
        except PermissionError:
            return None
        for child in sorted(children):
            result = find_release_folder(child, depth + 1)
            if result:
                return result
        return None
    try:
        subdirs = [c for c in path.iterdir() if c.is_dir()]
    except PermissionError:
        return path
    if len(subdirs) == 1:
        child = subdirs[0]
        if is_name_wrapper(name, child.name):
            return find_release_folder(child, depth + 1)
    return path


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
    s = re.sub(r"\s*-\s*\S+\.\w{2,4}\s*$", "", raw)
    s = re.sub(r"[._]", " ", s).strip()
    # Remove trailing year
    s = re.sub(r"\s+\d{4}$", "", s).strip()
    return s


# ── Destination suggestion ────────────────────────────────────────────────────

MATCH_AUTO = 0.80   # score above this → use existing folder name automatically
MATCH_HINT = 0.50   # score above this → suggest but mark uncertain


def suggest_destination(
    release_folder: Path,
    classification: dict,
    config: dict,
    library: "SeriesLibrary | None" = None,
) -> dict:
    rtype = classification["type"]

    if rtype == "series":
        raw_name  = classification["series_name"] or release_folder.name
        season    = classification["season"] or 1
        season_str = f"S{season:02d}"

        # ── Match against existing NAS library ────────────────────────────────
        matched_name  = None
        match_score   = 0.0
        match_auto    = False
        existing_seasons: list[str] = []

        if library:
            matched_name, match_score = library.match(raw_name)
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

        video_files = _find_videos(release_folder, config)

        return {
            "type": "series",
            "series_name": series_name,
            "season": season,
            "dest_dir": dest_dir,
            "is_season_pack": classification.get("is_season_pack", False),
            "video_files": [str(v.relative_to(release_folder)) for v in video_files],
            "confidence": classification["confidence"],
            # Library match info for the UI
            "matched_existing": matched_name if matched_name else None,
            "match_score": round(match_score, 2),
            "match_auto": match_auto,
            "existing_seasons": existing_seasons,
            "season_exists": season_exists,
            # All known series for the dropdown autocomplete
            "known_series": library.all_names() if library else [],
        }

    elif rtype == "movie":
        folder_name = release_folder.name
        first_char  = _first_letter(classification.get("title") or folder_name)
        dest_base   = os.path.join(config["movies_dir"], first_char, folder_name)
        title       = classification.get("title") or folder_name
        year        = classification.get("year")

        # Check for existing release of same title+year in the letter folder
        duplicate_path = _find_movie_duplicate(
            config["movies_dir"], first_char, title, year, folder_name
        )

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
        first_char  = _first_letter(folder_name)
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


def _title_key(name: str) -> str:
    """Normalize a folder/title for duplicate detection: strip group, quality, keep title+year."""
    s = re.sub(r"[._\-]", " ", name)
    # Remove everything from quality tag onward
    s = RE_QUALITY.split(s)[0]
    s = re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
    return s


def _find_movie_duplicate(
    movies_dir: str, first_char: str, title: str, year: int | None, new_folder: str
) -> str | None:
    """
    Look for an existing folder anywhere in movies_dir that matches
    the same title+year but is a different release (different group).
    Searches all letter subfolders to catch cases where old releases
    were filed under a different letter (e.g. The → T vs M).
    Returns the full path of the duplicate, or None.
    """
    movies_path = Path(movies_dir)
    if not movies_path.exists():
        return None

    new_key = _title_key(new_folder)

    try:
        # Iterate all single-letter (and #) subdirectories
        for letter_dir in sorted(movies_path.iterdir()):
            if not letter_dir.is_dir():
                continue
            if len(letter_dir.name) > 2:  # skip non-letter-index folders
                continue
            for entry in letter_dir.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name == new_folder:
                    continue  # exact same name → handled by dest_exists
                existing_key = _title_key(entry.name)
                if not existing_key or not new_key:
                    continue
                if year and str(year) not in entry.name:
                    continue
                score = _token_similarity(new_key, existing_key)
                if score >= 0.85:
                    return str(entry)
    except PermissionError:
        pass
    return None


def _find_videos(folder: Path, config: dict) -> list[Path]:
    exts = set(config.get("video_extensions", [".mkv", ".mp4", ".avi"]))
    videos = []
    for root, _, files in os.walk(folder):
        for f in files:
            if Path(f).suffix.lower() in exts:
                videos.append(Path(root) / f)
    return sorted(videos)


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan_downloads(config: dict) -> list[dict]:
    dl_dir = Path(config["download_dir"])
    if not dl_dir.exists():
        raise FileNotFoundError(f"Download directory not found: {dl_dir}")

    # Build library cache once per scan
    library = SeriesLibrary(config["series_dir"])

    items = []
    seen_releases: set[str] = set()

    for entry in sorted(dl_dir.iterdir()):
        try:
            if entry.is_file():
                ext = Path(entry.name).suffix.lower()
                if ext in set(config.get("video_extensions", [".mkv", ".mp4", ".avi"])):
                    item = _process_loose_file(entry, config, library)
                    if item:
                        items.append(item)

            elif entry.is_dir():
                release = find_release_folder(entry) or entry
                release_key = str(release.resolve())
                if release_key in seen_releases:
                    continue
                seen_releases.add(release_key)
                item = _process_release_folder(entry, release, config, library)
                if item:
                    items.append(item)

        except Exception as e:
            log.warning("Error processing %s: %s", entry, e)

    return items


def _process_release_folder(
    top_entry: Path, release: Path, config: dict, library: SeriesLibrary
) -> dict | None:
    classification = classify(release.name)
    suggestion     = suggest_destination(release, classification, config, library)
    dest_exists    = os.path.exists(suggestion["dest_dir"])

    return {
        "id":             str(top_entry.resolve()),
        "source_top":     str(top_entry),
        "source_release": str(release),
        "release_name":   release.name,
        "is_wrapper":     str(top_entry) != str(release),
        "wrapper_name":   top_entry.name if str(top_entry) != str(release) else None,
        "suggestion":     suggestion,
        "dest_exists":    dest_exists,
        "size_bytes":     _dir_size(release),
    }


def _process_loose_file(
    file: Path, config: dict, library: SeriesLibrary
) -> dict | None:
    classification = classify(file.stem)
    suggestion     = suggest_destination(file.parent / file.name, classification, config, library)

    if suggestion["type"] == "series":
        suggestion["video_files"] = [file.name]
        suggestion["dest_dir"] = os.path.join(
            config["series_dir"],
            suggestion.get("series_name", "Unknown"),
            f"S{suggestion.get('season', 1):02d}",
        )
    else:
        suggestion["dest_dir"] = os.path.join(
            config["movies_dir"],
            suggestion.get("first_char", "#"),
            file.name,
        )

    return {
        "id":             str(file.resolve()),
        "source_top":     str(file),
        "source_release": str(file),
        "release_name":   file.name,
        "is_wrapper":     False,
        "wrapper_name":   None,
        "suggestion":     suggestion,
        "dest_exists":    os.path.exists(suggestion["dest_dir"]),
        "size_bytes":     file.stat().st_size,
        "is_loose_file":  True,
    }


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total
