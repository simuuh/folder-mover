"""Configuration loader for Folder Mover."""

import os
import yaml

DEFAULT_CONFIG = {
    "download_dir": "/home/user/Downloads",
    "movies_dir": "/mnt/Filme",
    "series_dir": "/mnt/Serien",
    "port": 8080,
    "auth": {
        "username": "admin",
        "password": "changeme",
    },
    # Regex patterns to identify episode files/folders
    # Group 1: series name part, Group 2: season number, Group 3: episode number(s)
    "episode_patterns": [
        r"(?i)S(\d{1,2})E(\d{1,2})",          # S01E01
        r"(?i)[._\- ](\d{1,2})x(\d{2})",       # 1x01
        r"(?i)[._\- ](Folge|Episode|Ep)[._\- ]?(\d{1,3})",  # Folge 01
    ],
    # Folder name patterns that indicate a wrapper (not the real release)
    "wrapper_patterns": [
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",  # UUID
    ],
    # Extensions considered video files (used to find the actual content)
    "video_extensions": [".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts"],
}


def load_config(path: str = None) -> dict:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config.yaml")

    config = dict(DEFAULT_CONFIG)
    config["auth"] = dict(DEFAULT_CONFIG["auth"])

    if os.path.exists(path):
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        # Deep merge top-level keys
        for key, val in user_cfg.items():
            if isinstance(val, dict) and key in config and isinstance(config[key], dict):
                config[key].update(val)
            else:
                config[key] = val

    return config
