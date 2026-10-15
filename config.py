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
        log.warning("Config file %s not found — using defaults", path)

    return config


def video_extensions(config: dict) -> set[str]:
    """Configured video extensions, normalised to lowercase with a leading dot."""
    exts = config.get("video_extensions") or DEFAULT_CONFIG["video_extensions"]
    return {e.lower() if e.startswith(".") else "." + e.lower() for e in exts}
