"""History: persists move operations to a JSON-lines log file."""

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

HISTORY_FILE = Path(os.environ.get("FOLDER_MOVER_HISTORY") or Path(__file__).parent / "history.jsonl")
# Default for config `history_max` (0 = unlimited). One entry is ~400 bytes,
# so 10 000 entries are ~4 MB — reading that per request is still instant.
MAX_ENTRIES = 10_000

FILTERS = ("all", "movie", "series", "error")


def append(moves: list[dict], results: list[dict], max_entries: int = MAX_ENTRIES):
    """
    Write one log entry per move result.
    moves and results are parallel lists (same order as execute_moves input/output).
    """
    entries = []
    for move, result in zip(moves, results, strict=False):
        if result.get("skipped"):
            continue   # never started (move cancelled before it) — nothing happened
        entry = {
            "ts":            datetime.now(UTC).isoformat(),
            "release_name":  Path(move.get("source_release", "")).name,
            "type":          move.get("type", "unknown"),
            "source":        move.get("source_release", ""),
            "dest":          move.get("dest_dir", ""),
            "ok":            result.get("ok", False),
            "error":         result.get("error") if not result.get("ok") else None,
            # i18n keys of the error → shown in the language the user has *now*
            "error_i18n":    result.get("error_i18n") if not result.get("ok") else None,
            # warnings of a successful move (e.g. episode skipped because it already existed)
            "warnings_i18n": result.get("warnings_i18n") if result.get("ok") else None,
            "replaced_path": result.get("replaced_path"),  # old NAS release deleted after this move
            "size_bytes":    result.get("size_bytes"),
        }
        entries.append(entry)

    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        _trim(max_entries)
    except Exception as ex:
        log.warning("Could not write history: %s", ex)


def _entries_newest_first() -> Iterator[dict]:
    if not HISTORY_FILE.exists():
        return
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except Exception as ex:
        log.warning("Could not read history: %s", ex)
        return
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry


def _matches(entry: dict, terms: list[str], type_filter: str) -> bool:
    if type_filter == "error" and entry.get("ok"):
        return False
    if type_filter in ("movie", "series") and entry.get("type") != type_filter:
        return False
    haystack = f"{entry.get('release_name') or ''}\n{entry.get('dest') or ''}".lower()
    return all(term in haystack for term in terms)


def query(search: str = "", type_filter: str = "all", offset: int = 0, limit: int = 100) -> tuple[list[dict], int]:
    """
    One page of entries (newest first) matching `search` (every word must occur
    in the release name or destination, case-insensitive) and `type_filter`
    (one of FILTERS). Returns (page, number of all matching entries).
    """
    terms = search.lower().split()
    page, matched = [], 0
    for entry in _entries_newest_first():
        if not _matches(entry, terms, type_filter):
            continue
        if offset <= matched < offset + limit:
            page.append(entry)
        matched += 1
    return page, matched


def _month_keys(months: int, now: datetime) -> list[str]:
    """'YYYY-MM' of the last `months` months, oldest first, ending with `now`'s month."""
    year, month = now.year, now.month
    keys = []
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return keys[::-1]


def stats(months: int = 12, now: datetime | None = None) -> dict:
    """
    Totals over the whole history plus moved bytes/entries per month for the
    last `months` months (zero-filled, UTC). Only successful moves count as moved.
    """
    by_month = {key: {"month": key, "bytes": 0, "count": 0} for key in _month_keys(months, now or datetime.now(UTC))}
    totals = {"moved": 0, "failed": 0, "bytes": 0, "movies": 0, "series": 0}
    for entry in _entries_newest_first():
        if not entry.get("ok"):
            totals["failed"] += 1
            continue
        size = entry.get("size_bytes") if isinstance(entry.get("size_bytes"), int) else 0
        totals["moved"] += 1
        totals["bytes"] += size
        if entry.get("type") == "movie":
            totals["movies"] += 1
        elif entry.get("type") == "series":
            totals["series"] += 1
        bucket = by_month.get(str(entry.get("ts") or "")[:7])
        if bucket:
            bucket["bytes"] += size
            bucket["count"] += 1
    return {**totals, "months": list(by_month.values())}


def read(limit: int = 200) -> list[dict]:
    """Return the most recent `limit` entries, newest first."""
    return query(limit=limit)[0]


def count() -> int:
    """Total number of stored entries (independent of any read limit)."""
    if not HISTORY_FILE.exists():
        return 0
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception as ex:
        log.warning("Could not count history: %s", ex)
        return 0


def _trim(max_entries: int):
    """Keep only the last `max_entries` lines (0 = keep everything)."""
    if not max_entries or max_entries <= 0:
        return
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) <= max_entries:
            return
        # Write next to it and swap: a crash or full disk mid-write must not
        # cost the whole history
        tmp = HISTORY_FILE.with_name(HISTORY_FILE.name + ".tmp")
        tmp.write_text("\n".join(lines[-max_entries:]) + "\n", encoding="utf-8")
        os.replace(tmp, HISTORY_FILE)
    except Exception as ex:
        log.warning("Could not trim history: %s", ex)
