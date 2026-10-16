import os
import tempfile
from pathlib import Path

import pytest

# Keep history.jsonl / .secret_key of the imported app out of the repo
os.environ.setdefault("FOLDER_MOVER_HISTORY", os.path.join(tempfile.mkdtemp(), "history.jsonl"))

UUID_A = "6d5b82f9-1111-2222-3333-444455556666"
UUID_B = "aaaaaaaa-1111-2222-3333-444455556666"


def make_files(base: Path, *rel_paths: str, size: int = 16) -> None:
    """Create files (and parent folders) below `base`."""
    for rel in rel_paths:
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)


@pytest.fixture
def dirs(tmp_path):
    d = {
        "download_dir": tmp_path / "downloads",
        "movies_dir": tmp_path / "Filme",
        "series_dir": tmp_path / "Serien",
    }
    for p in d.values():
        p.mkdir()
    return d


@pytest.fixture
def config(dirs):
    return {
        **{k: str(v) for k, v in dirs.items()},
        "video_extensions": [".mkv", ".mp4"],
        "wrapper_patterns": [],
    }
