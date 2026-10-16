"""Configuration loader for Folder Mover."""

import logging
import os

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
    # Stable secret for session cookies. If empty, a random key is generated on
    # every start (everyone gets logged out on restart).
    "secret_key": "",
    "auth": {
        "username": "admin",
        # Either set a plaintext `password` or (preferred) a `password_hash`
        # generated with `python app.py --hash-password`.
        "password": "changeme",
        "password_hash": "",
    },
    # Additional regexes for folder names that are pure wrappers (not the real
    # release). UUID folders are always treated as wrappers.
    "wrapper_patterns": [],
    # Extensions considered video files (used to find the actual content)
    "video_extensions": [".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts"],
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
    "FOLDER_MOVER_SECRET_KEY":    ("secret_key",),
    "FOLDER_MOVER_USERNAME":      ("auth", "username"),
    "FOLDER_MOVER_PASSWORD":      ("auth", "password"),
    "FOLDER_MOVER_PASSWORD_HASH": ("auth", "password_hash"),
}


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
        target[keys[-1]] = int(val) if keys[-1] == "port" else val

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


def video_extensions(config: dict) -> set[str]:
    """Configured video extensions, normalised to lowercase with a leading dot."""
    exts = config.get("video_extensions") or DEFAULT_CONFIG["video_extensions"]
    return {e.lower() if e.startswith(".") else "." + e.lower() for e in exts}
