"""Configuration loader for Folder Mover."""

import fnmatch
import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "download_dir": "/home/user/Downloads",
    "movies_dir": "/mnt/Filme",
    "series_dir": "/mnt/Serien",
    "host": "0.0.0.0",
    "port": 8080,
    # UI language: "auto" (browser language), or a catalog name like "de" / "en".
    # The language switch in the header overrides this per browser.
    "language": "auto",
    # Stable secret for session cookies. If empty, one is generated once and
    # stored in ".secret_key" next to history.jsonl.
    "secret_key": "",
    # Sessions end after this many days (and on logout / password change).
    "session_days": 30,
    # Number of reverse proxies in front of the app (0 = none). Needed so the
    # login lockout sees the real client IP instead of the proxy's.
    "proxy_count": 0,
    "auth": {
        "username": "admin",
        # Either set a plaintext `password` or (preferred) a `password_hash`
        # generated with `python app.py --hash-password`.
        "password": "changeme",
        "password_hash": "",
    },
    # Name of a file/folder that must exist in movies_dir AND series_dir before
    # anything is moved (e.g. ".folder-mover"). Protects against writing into
    # the empty mountpoint of an NAS share that is not mounted. Empty = off.
    "mount_marker": "",
    # Additional regexes for folder names that are pure wrappers (not the real
    # release). UUID folders are always treated as wrappers.
    "wrapper_patterns": [],
    # Extensions considered video files (used to find the actual content)
    "video_extensions": [".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts"],
    # Glob patterns (case-insensitive) for junk that is never copied to the NAS
    # and deleted with the download: hoster shortcuts, checker logs, release
    # banners. Matched against every file and folder name inside a release.
    # Setting this replaces the defaults; [] copies everything.
    "junk_patterns": [
        "*.url", "*.lnk", "*.website", "*.webloc", "*.desktop",
        "rushchk.log", "*- COMPLETE )*",
        "thumbs.db", "desktop.ini", ".ds_store",
    ],
}

DEFAULT_PASSWORD = DEFAULT_CONFIG["auth"]["password"]

# Suffix of files/folders the mover is still writing (renamed when complete)
PARTIAL_SUFFIX = ".fmpart"

# Environment variables override config.yaml (handy for Docker)
ENV_OVERRIDES = {
    "FOLDER_MOVER_DOWNLOAD_DIR":  ("download_dir",),
    "FOLDER_MOVER_MOVIES_DIR":    ("movies_dir",),
    "FOLDER_MOVER_SERIES_DIR":    ("series_dir",),
    "FOLDER_MOVER_HOST":          ("host",),
    "FOLDER_MOVER_PORT":          ("port",),
    "FOLDER_MOVER_LANGUAGE":      ("language",),
    "FOLDER_MOVER_MOUNT_MARKER":  ("mount_marker",),
    "FOLDER_MOVER_SECRET_KEY":    ("secret_key",),
    "FOLDER_MOVER_SESSION_DAYS":  ("session_days",),
    "FOLDER_MOVER_PROXY_COUNT":   ("proxy_count",),
    "FOLDER_MOVER_USERNAME":      ("auth", "username"),
    "FOLDER_MOVER_PASSWORD":      ("auth", "password"),
    "FOLDER_MOVER_PASSWORD_HASH": ("auth", "password_hash"),
}


INT_KEYS = {"port", "session_days", "proxy_count"}


def load_config(path: str | None = None) -> dict:
    if path is None:
        path = os.environ.get("FOLDER_MOVER_CONFIG") or os.path.join(
            os.path.dirname(__file__), "config.yaml"
        )

    config = dict(DEFAULT_CONFIG)
    config["auth"] = dict(DEFAULT_CONFIG["auth"])

    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        # Deep merge top-level keys
        for key, val in user_cfg.items():
            if isinstance(val, dict) and key in config and isinstance(config[key], dict):
                config[key].update(val)
            else:
                config[key] = val
    else:
        log.info("Config file %s not found — using defaults and environment", path)

    for env, keys in ENV_OVERRIDES.items():
        val = os.environ.get(env)
        if not val:
            continue
        target = config
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = int(val) if keys[-1] in INT_KEYS else val

    return config


DIR_KEYS = ("download_dir", "movies_dir", "series_dir")


def dir_overlaps(config: dict) -> list[tuple[str, str]]:
    """
    Pairs of configured directories that are equal or nested in each other.
    Such a setup is refused: the scanner would offer the media library as
    "downloads", and moves/deletes could hit the library.
    """
    dirs = {key: os.path.realpath(config[key]) for key in DIR_KEYS}
    overlaps = []
    for i, a in enumerate(DIR_KEYS):
        for b in DIR_KEYS[i + 1:]:
            try:
                common = os.path.commonpath([dirs[a], dirs[b]])
            except ValueError:   # different drives on Windows
                continue
            if common in (dirs[a], dirs[b]):
                overlaps.append((a, b))
    return overlaps


def check_config(config: dict) -> list[str]:
    """Human-readable problems with the configured directories (logged at startup)."""
    problems = []
    for key in DIR_KEYS:
        path = os.path.realpath(config[key])
        if not os.path.isdir(path):
            problems.append(f"{key} does not exist: {path}")
    for a, b in dir_overlaps(config):
        problems.append(f"{a} and {b} overlap ({os.path.realpath(config[a])} / {os.path.realpath(config[b])}) "
                        "— scan, move and delete are disabled until this is fixed")
    return problems


MEDIA_KEYS = ("movies_dir", "series_dir")


def missing_mount_markers(config: dict) -> list[str]:
    """Media directories in which the configured `mount_marker` does not exist."""
    marker = config.get("mount_marker") or ""
    if not marker:
        return []
    return [config[key] for key in MEDIA_KEYS if not os.path.lexists(os.path.join(config[key], marker))]


def library_warnings(config: dict) -> list[dict]:
    """
    i18n warnings ({key, args}) about media directories that look unmounted:
    marker missing, folder missing, or completely empty.
    """
    warnings = []
    missing = set(missing_mount_markers(config))
    for key in MEDIA_KEYS:
        path = config[key]
        if path in missing:
            warnings.append({"key": "scan.marker_missing", "args": {"path": path, "marker": config["mount_marker"]}})
        elif not os.path.isdir(path):
            warnings.append({"key": "scan.library_missing", "args": {"path": path}})
        else:
            try:
                empty = not any(not e.name.startswith((".", "@")) for e in os.scandir(path))
            except OSError:
                empty = False
            if empty:
                warnings.append({"key": "scan.library_empty", "args": {"path": path}})
    return warnings


def video_extensions(config: dict) -> set[str]:
    """Configured video extensions, normalised to lowercase with a leading dot."""
    exts = config.get("video_extensions") or DEFAULT_CONFIG["video_extensions"]
    return {e.lower() if e.startswith(".") else "." + e.lower() for e in exts}


def is_junk(rel_path: Path, config: dict) -> bool:
    """True if any part of `rel_path` (relative to the release) matches `junk_patterns`."""
    patterns = config.get("junk_patterns")
    if patterns is None:
        patterns = DEFAULT_CONFIG["junk_patterns"]
    return any(fnmatch.fnmatchcase(part.lower(), pat.lower())
               for part in rel_path.parts for pat in patterns)
