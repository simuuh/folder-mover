"""History: persists move operations to a JSON-lines log file."""

import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

HISTORY_FILE = Path(__file__).parent / "history.jsonl"
MAX_ENTRIES = 500


def append(moves: list[dict], results: list[dict]):
    """
    Write one log entry per move result.
    moves and results are parallel lists (same order as execute_moves input/output).
    """
    entries = []
    for move, result in zip(moves, results):
        entry = {
            "ts":            datetime.now(timezone.utc).isoformat(),
            "release_name":  Path(move.get("source_release", "")).name,
            "type":          move.get("type", "unknown"),
            "source":        move.get("source_release", ""),
            "dest":          move.get("dest_dir", ""),
            "ok":            result.get("ok", False),
            "error":         result.get("error") if not result.get("ok") else None,
            "replaced_path": move.get("replaced_path"),  # set when replacing existing NAS release
            "size_bytes":    None,
        }
        entries.append(entry)

    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        _trim()
    except Exception as ex:
        log.warning("Could not write history: %s", ex)


def read(limit: int = 200) -> list[dict]:
    """Return the most recent `limit` entries, newest first."""
    if not HISTORY_FILE.exists():
        return []
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        entries = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= limit:
                break
        return entries
    except Exception as ex:
        log.warning("Could not read history: %s", ex)
        return []


def _trim():
    """Keep only the last MAX_ENTRIES lines."""
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_ENTRIES:
            HISTORY_FILE.write_text(
                "\n".join(lines[-MAX_ENTRIES:]) + "\n", encoding="utf-8"
            )
    except Exception as ex:
        log.warning("Could not trim history: %s", ex)
