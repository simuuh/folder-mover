"""Mover: executes confirmed move operations with live progress reporting."""

import os
import shutil
import time
import threading
import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB read chunks

# ── Global progress state (single-user tool) ──────────────────────────────────
_progress_lock = threading.Lock()
_progress: dict = {}


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def _set(**kwargs):
    with _progress_lock:
        _progress.update(kwargs)


def reset_progress():
    with _progress_lock:
        _progress.clear()


# ── Public entry point ────────────────────────────────────────────────────────

def execute_moves(moves: list[dict], config: dict) -> list[dict]:
    """Execute moves with live progress updates via get_progress()."""
    total_bytes = sum(_calc_size(m) for m in moves)
    copied_bytes = 0

    _set(
        active=True,
        total_items=len(moves),
        done_items=0,
        total_bytes=total_bytes,
        copied_bytes=0,
        current_file="",
        current_item="",
        current_item_bytes=0,
        current_item_total=0,
        speed_bps=0,
        eta_seconds=None,
        errors=[],
        finished=False,
    )

    results = []
    tracker = _SpeedTracker()

    for idx, move in enumerate(moves):
        release_name = Path(move["source_release"]).name
        _set(current_item=release_name, done_items=idx)

        try:
            def on_chunk(fname: str, item_copied: int, item_total: int, chunk: int):
                nonlocal copied_bytes
                copied_bytes += chunk
                speed = tracker.speed(copied_bytes)
                eta = ((total_bytes - copied_bytes) / speed) if speed > 0 else None
                _set(
                    current_file=fname,
                    current_item_bytes=item_copied,
                    current_item_total=item_total,
                    copied_bytes=copied_bytes,
                    speed_bps=speed,
                    eta_seconds=round(eta) if eta is not None else None,
                )

            result = _do_move(move, config, on_chunk)
            results.append(result)
            if not result["ok"]:
                with _progress_lock:
                    _progress["errors"].append(result.get("error", "Unknown error"))

        except Exception as e:
            log.exception("Move failed for %s", move.get("source_release"))
            results.append({"source_release": move.get("source_release"), "ok": False, "error": str(e)})
            with _progress_lock:
                _progress["errors"].append(str(e))

    _set(
        active=False,
        finished=True,
        done_items=len(moves),
        copied_bytes=total_bytes,
        current_file="",
        current_item="Fertig",
        speed_bps=0,
        eta_seconds=0,
    )
    return results


# ── Move logic ────────────────────────────────────────────────────────────────

def _do_move(move: dict, config: dict, on_chunk: Callable) -> dict:
    source_release = Path(move["source_release"])
    source_top     = Path(move["source_top"])
    dest_dir       = Path(move["dest_dir"])
    move_type      = move.get("type", "movie")
    is_loose       = move.get("is_loose_file", False)
    video_files    = move.get("video_files", [])

    if not source_release.exists():
        return {"source_release": str(source_release), "ok": False, "error": "Source no longer exists"}

    # ── Series: move only video files ─────────────────────────────────────────
    if move_type == "series":
        # Season folder may already exist (adding episodes) — that's fine
        dest_dir.mkdir(parents=True, exist_ok=True)
        moved, errors = [], []

        if not video_files:
            exts = set(config.get("video_extensions", [".mkv", ".mp4", ".avi"]))
            if source_release.is_file():
                video_files = [source_release.name]
                source_base = source_release.parent
            else:
                source_base = source_release
                for root, _, files in os.walk(source_release):
                    for f in files:
                        if Path(f).suffix.lower() in exts:
                            video_files.append(str(Path(root).relative_to(source_release) / f))
        else:
            source_base = source_release if source_release.is_dir() else source_release.parent

        item_total = sum(
            (source_base / vf).stat().st_size
            for vf in video_files if (source_base / vf).exists()
        )
        item_copied = 0

        for rel_path in video_files:
            src_file = source_base / rel_path if not is_loose else source_release
            dst_file = dest_dir / Path(rel_path).name

            if not src_file.exists():
                errors.append(f"File not found: {rel_path}")
                continue
            if dst_file.exists():
                errors.append(f"Destination exists: {dst_file.name}")
                continue

            log.info("Copying series file: %s → %s", src_file, dst_file)
            offset = item_copied

            def _cb(fname, ic, it, chunk, _off=offset):
                on_chunk(fname, _off + ic, item_total, chunk)

            _copy_with_progress(src_file, dst_file, _cb)
            item_copied += src_file.stat().st_size
            src_file.unlink(missing_ok=True)
            moved.append(str(dst_file))

        _cleanup_after_series_move(source_top, source_release, config)

        if errors and not moved:
            return {"source_release": str(source_release), "ok": False, "error": "; ".join(errors)}
        return {"source_release": str(source_release), "ok": True,
                "moved_files": moved, "warnings": errors or None}

    # ── Movie / Unknown: copy entire release folder ───────────────────────────
    else:
        dest_dir.parent.mkdir(parents=True, exist_ok=True)

        if dest_dir.exists():
            return {"source_release": str(source_release), "ok": False,
                    "error": f"Destination already exists: {dest_dir}"}

        src = source_release
        if src.is_file():
            files_to_copy = [(src, dest_dir)]
            item_total = src.stat().st_size
        else:
            files_to_copy = []
            item_total = 0
            for root, _, fnames in os.walk(src):
                for fname in fnames:
                    fsrc = Path(root) / fname
                    fdst = dest_dir / fsrc.relative_to(src)
                    files_to_copy.append((fsrc, fdst))
                    item_total += fsrc.stat().st_size

        item_copied = 0
        for fsrc, fdst in files_to_copy:
            fdst.parent.mkdir(parents=True, exist_ok=True)
            offset = item_copied

            def _cb(fname, ic, it, chunk, _off=offset):
                on_chunk(fname, _off + ic, item_total, chunk)

            log.info("Copying: %s → %s", fsrc, fdst)
            _copy_with_progress(fsrc, fdst, _cb)
            item_copied += fsrc.stat().st_size
            fsrc.unlink(missing_ok=True)

        # Remove now-empty source tree
        if src.exists():
            shutil.rmtree(str(src), ignore_errors=True)
        if source_top != source_release and source_top.exists():
            _remove_if_empty(source_top)

        return {"source_release": str(source_release), "ok": True, "dest": str(dest_dir)}


# ── Chunked file copy ─────────────────────────────────────────────────────────

def _copy_with_progress(src: Path, dst: Path, callback: Callable):
    """Copy src → dst in 4 MB chunks, firing callback per chunk after actual write."""
    total = src.stat().st_size
    copied = 0
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            buf = fsrc.read(CHUNK_SIZE)
            if not buf:
                break
            fdst.write(buf)
            # Force flush to SMB/NFS so timing reflects actual network speed
            fdst.flush()
            os.fsync(fdst.fileno())
            chunk = len(buf)
            copied += chunk
            callback(src.name, copied, total, chunk)
    shutil.copystat(str(src), str(dst))


# ── Rolling speed tracker ─────────────────────────────────────────────────────

class _SpeedTracker:
    def __init__(self, window: float = 4.0):
        self._start = time.monotonic()
        self._samples: list[tuple[float, int]] = []
        self._window = window
        self._last_speed = 0.0

    def speed(self, total_copied: int) -> float:
        now = time.monotonic()
        elapsed = now - self._start

        # Don't report until at least 1s has passed
        if elapsed < 1.0:
            return 0.0

        self._samples.append((now, total_copied))
        # Keep only samples within the rolling window
        cutoff = now - self._window
        self._samples = [(t, b) for t, b in self._samples if t >= cutoff]

        if len(self._samples) >= 2:
            dt = self._samples[-1][0] - self._samples[0][0]
            db = self._samples[-1][1] - self._samples[0][1]
            if dt >= 0.5:
                raw = db / dt
                # Smooth: 70% new value, 30% last known (avoid spikes)
                self._last_speed = 0.7 * raw + 0.3 * self._last_speed
                return self._last_speed

        # Fallback: average since start
        self._last_speed = total_copied / elapsed
        return self._last_speed


# ── Helpers ───────────────────────────────────────────────────────────────────

def _calc_size(move: dict) -> int:
    src = Path(move["source_release"])
    if not src.exists():
        return 0
    if src.is_file():
        return src.stat().st_size
    total = 0
    for root, _, files in os.walk(src):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _cleanup_after_series_move(source_top: Path, source_release: Path, config: dict):
    exts = set(config.get("video_extensions", [".mkv", ".mp4", ".avi"]))
    has_video = any(
        Path(f).suffix.lower() in exts
        for root, _, files in os.walk(source_release) if source_release.exists()
        for f in files
    )
    if not has_video and source_release.exists():
        log.info("Removing video-less source: %s", source_release)
        shutil.rmtree(str(source_release), ignore_errors=True)
        if source_top != source_release and source_top.exists():
            _remove_if_empty(source_top)


def _remove_if_empty(path: Path):
    try:
        if not any(f.is_file() for f in path.rglob("*")):
            shutil.rmtree(str(path), ignore_errors=True)
    except Exception as e:
        log.warning("Could not clean up %s: %s", path, e)
