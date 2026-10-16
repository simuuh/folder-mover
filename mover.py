"""Mover: executes confirmed move operations with live progress reporting."""

import errno
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from config import PARTIAL_SUFFIX
from scanner import RE_SEASON_EP, RE_SxE, find_videos

log = logging.getLogger(__name__)

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB read chunks
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}
SUBTITLE_DIRS = ("Subs", "subs", "Subtitles", "subtitles")
FSYNC_EVERY_BYTES = 64 * 1024 * 1024  # fsync at most every 64 MB (not every chunk)

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


def _err(key: str, text: str, **args) -> dict:
    """
    One error: an i18n key + args (translated in the browser, see
    translations/*.json) and an English text for logs and API consumers.
    """
    return {"key": key, "args": args, "text": text}


def _warnings(warnings: list[dict]) -> dict:
    """`warnings` / `warnings_i18n` of a successful result (same shape as errors)."""
    return {"warnings": [w["text"] for w in warnings] or None,
            "warnings_i18n": [{"key": w["key"], "args": w["args"]} for w in warnings] or None}


class _SourceChanged(Exception):
    """The source release changed while it was being copied."""


def _fail(source_release, *errors: dict) -> dict:
    return {"source_release": str(source_release), "ok": False,
            "error": "; ".join(e["text"] for e in errors),
            "error_i18n": [{"key": e["key"], "args": e["args"]} for e in errors]}


def _progress_error(result: dict) -> dict:
    """Shape of an entry in the progress `errors` list (same keys as a result)."""
    return {"error": result.get("error", "Unknown error"), "error_i18n": result.get("error_i18n")}


def mark_pending(total_items: int):
    """Mark a move as started before the worker thread is up (avoids SSE races)."""
    with _progress_lock:
        _progress.clear()
        _progress.update(active=True, finished=False, total_items=total_items,
                         done_items=0, total_bytes=0, copied_bytes=0, errors=[])


def mark_failed(error: str):
    """Finish the progress state after an unexpected crash of the worker."""
    with _progress_lock:
        _progress.setdefault("errors", []).append({"error": error, "error_i18n": None})
        _progress.update(active=False, finished=True, speed_bps=0, eta_seconds=0)


# ── Public entry point ────────────────────────────────────────────────────────

def execute_moves(
    moves: list[dict], config: dict, on_done: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """
    Execute moves with live progress updates via get_progress().

    `on_done(results)` runs BEFORE the progress state switches to finished, so
    anything the client reloads on "finished" (results, history) already exists.
    """
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
                    _progress["errors"].append(_progress_error(result))

        except Exception as e:
            log.exception("Move failed for %s", move.get("source_release"))
            result = _fail(move.get("source_release"), _err("mover.unexpected", str(e), error=str(e)))
            results.append(result)
            with _progress_lock:
                _progress["errors"].append(_progress_error(result))

    if on_done:
        try:
            on_done(results)
        except Exception:
            log.exception("on_done callback failed")

    _set(
        active=False,
        finished=True,
        done_items=len(moves),
        copied_bytes=total_bytes,
        current_file="",
        current_item="",
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
        return _fail(source_release, _err("mover.source_missing", "Source no longer exists"))

    # ── Series: move only video files ─────────────────────────────────────────
    if move_type == "series":
        # Season folder may already exist (adding episodes) — that's fine
        dest_dir.mkdir(parents=True, exist_ok=True)
        moved, errors = [], []

        if not video_files:
            if source_release.is_file():
                video_files = [source_release.name]
                source_base = source_release.parent
            else:
                source_base = source_release
                video_files = [v.relative_to(source_release).as_posix()
                               for v in find_videos(source_release, config)]
        else:
            source_base = source_release if source_release.is_dir() else source_release.parent

        item_total = sum(
            (source_base / vf).stat().st_size
            for vf in video_files if (source_base / vf).exists()
        )
        item_copied = 0
        space_checked = False
        failed = False

        for rel_path in video_files:
            src_file = source_base / rel_path if not is_loose else source_release
            dst_file = dest_dir / _episode_file_name(rel_path)

            if not src_file.exists():
                errors.append(_err("mover.file_not_found", f"File not found: {rel_path}", name=rel_path))
                continue
            if dst_file.exists():
                errors.append(_err("mover.dest_file_exists", f"Destination exists: {dst_file.name}",
                                   name=dst_file.name))
                continue

            size = src_file.stat().st_size
            if _try_rename(src_file, dst_file):
                item_copied += size
                on_chunk(src_file.name, item_copied, item_total, size)
                moved.append(str(dst_file))
                _move_subtitles(src_file, dst_file, single_video=len(video_files) == 1 and not is_loose)
                continue

            # Checked before the first real copy — also when source and target
            # share a device but rename() still fails (two Docker bind mounts)
            if not space_checked:
                space_checked = True
                space_error = _check_free_space(dest_dir, item_total - item_copied)
                if space_error:
                    errors.append(space_error)
                    failed = True
                    break

            log.info("Copying series file: %s → %s", src_file, dst_file)
            offset = item_copied
            before = _stat_key(src_file)

            def _cb(fname, ic, it, chunk, _off=offset):
                on_chunk(fname, _off + ic, item_total, chunk)

            try:
                _copy_with_progress(src_file, dst_file, _cb)
            except OSError as e:
                # Episodes moved so far stay moved; report which one failed and stop
                log.warning("Copy failed: %s → %s: %s", src_file, dst_file, e)
                errors.append(_err("mover.copy_failed", f"Copy failed: {rel_path} ({e})",
                                   name=rel_path, error=e.strerror or str(e)))
                failed = True
                break
            if _stat_key(src_file) != before:
                # Still being written: the copy may be truncated → keep the source
                dst_file.unlink(missing_ok=True)
                errors.append(_err("mover.file_changed", f"File changed during the copy: {rel_path}",
                                   name=rel_path))
                continue
            item_copied += before[0]
            src_file.unlink(missing_ok=True)
            moved.append(str(dst_file))
            _move_subtitles(src_file, dst_file, single_video=len(video_files) == 1 and not is_loose)

        _cleanup_after_series_move(source_top, source_release, config)

        if failed or (errors and not moved):
            # Partial moves list what already made it to the NAS
            return {**_fail(source_release, *errors), "moved_files": moved, "size_bytes": item_copied}
        return {"source_release": str(source_release), "ok": True, "size_bytes": item_copied,
                "moved_files": moved, **_warnings(errors)}

    # ── Movie / Unknown: copy entire release folder ───────────────────────────
    else:
        dest_dir.parent.mkdir(parents=True, exist_ok=True)

        if dest_dir.exists():
            return _fail(source_release, _err("mover.dest_exists", f"Destination already exists: {dest_dir}",
                                              path=str(dest_dir)))

        src = source_release
        # What exists now is exactly what gets copied — and later deleted
        snapshot = _snapshot(src)
        if src.is_file():
            files_to_copy = [(src, dest_dir)]
        else:
            files_to_copy = [(fsrc, dest_dir / fsrc.relative_to(src)) for fsrc in snapshot]
        item_total = sum(size for size, _ in snapshot.values())

        # Same filesystem (e.g. Docker on the NAS): one atomic rename, no copy
        if _try_rename(src, dest_dir):
            on_chunk(src.name, item_total, item_total, item_total)
            if source_top != source_release and source_top.exists():
                _remove_if_empty(source_top)
            return {"source_release": str(source_release), "ok": True, "dest": str(dest_dir),
                    "size_bytes": item_total, "renamed": True}

        space_error = _check_free_space(dest_dir, item_total)
        if space_error:
            return _fail(source_release, space_error)

        # Copy everything into "<dest>.fmpart/" first and only give it its
        # final name (and delete the source) once ALL files are on the NAS —
        # a failure or crash halfway never leaves a release that looks complete.
        staging = dest_dir if src.is_file() else _partial_path(dest_dir)
        if staging != dest_dir:
            if staging.exists():
                shutil.rmtree(str(staging), ignore_errors=True)   # leftover of an interrupted move
            files_to_copy = [(fsrc, staging / fdst.relative_to(dest_dir)) for fsrc, fdst in files_to_copy]
        item_copied = 0
        try:
            for fsrc, fdst in files_to_copy:
                fdst.parent.mkdir(parents=True, exist_ok=True)
                offset = item_copied

                def _cb(fname, ic, it, chunk, _off=offset):
                    on_chunk(fname, _off + ic, item_total, chunk)

                log.info("Copying: %s → %s", fsrc, fdst)
                _copy_with_progress(fsrc, fdst, _cb)
                item_copied += fsrc.stat().st_size
            if _snapshot(src) != snapshot:
                # Still being written/extracted: the copy may be incomplete
                raise _SourceChanged
            if staging != dest_dir:
                staging.mkdir(exist_ok=True)   # release without any files
                if dest_dir.exists():
                    raise FileExistsError(errno.EEXIST, "Destination exists", str(dest_dir))
                os.replace(staging, dest_dir)   # same folder → never EXDEV
        except BaseException as e:
            # staging belongs to this move only → safe to roll back
            log.warning("Copy failed, rolling back %s", staging)
            if staging.is_dir():
                shutil.rmtree(str(staging), ignore_errors=True)
            elif isinstance(e, _SourceChanged):
                staging.unlink(missing_ok=True)   # loose file: copied by this move, now outdated
            if isinstance(e, _SourceChanged):
                return _fail(source_release, _err(
                    "mover.source_changed", "Download is still being written (files changed during the copy)"))
            raise

        # Delete only what was copied; files that appeared meanwhile stay
        for fsrc, _ in files_to_copy:
            fsrc.unlink(missing_ok=True)
        warnings = []
        if src.is_dir():
            _remove_empty_dirs(src)
            if src.exists():
                log.warning("New files appeared during the move, kept: %s", src)
                warnings.append(_err("mover.source_leftover", f"Files that appeared during the move were kept: {src}",
                                     path=str(src)))
        if source_top != source_release and source_top.exists():
            _remove_if_empty(source_top)

        return {"source_release": str(source_release), "ok": True, "dest": str(dest_dir),
                "size_bytes": item_total, **_warnings(warnings)}


# ── Chunked file copy ─────────────────────────────────────────────────────────

def _partial_path(dst: Path) -> Path:
    return dst.with_name(dst.name + PARTIAL_SUFFIX)


def _copy_with_progress(src: Path, dst: Path, callback: Callable):
    """
    Copy src → dst in 4 MB chunks, firing callback per chunk after actual write.

    The data is written to "<dst>.fmpart" and only renamed to `dst` once it is
    complete and synced. If the process dies mid-copy (SIGKILL, OOM, power
    loss, `docker stop`), only a clearly unfinished .fmpart file is left —
    never a truncated file under the final name.

    fsync() is only forced every FSYNC_EVERY_BYTES (plus once at the end),
    not after every single 4 MB chunk. fsync on SMB/NFS is a blocking round-trip,
    so syncing on every chunk turns a fast network copy into one limited by
    sync latency rather than actual throughput. Periodic syncing still keeps
    the progress/speed numbers reasonably accurate (re-anchored every ~64 MB)
    while avoiding hundreds of extra round-trips per file.
    """
    if dst.exists():
        raise FileExistsError(errno.EEXIST, "Destination exists", str(dst))
    tmp = _partial_path(dst)
    tmp.unlink(missing_ok=True)   # leftover of an interrupted earlier copy
    total = src.stat().st_size
    copied = 0
    since_last_sync = 0
    try:
        with open(src, "rb") as fsrc, open(tmp, "xb") as fdst:
            while True:
                buf = fsrc.read(CHUNK_SIZE)
                if not buf:
                    break
                fdst.write(buf)
                chunk = len(buf)
                copied += chunk
                since_last_sync += chunk

                if since_last_sync >= FSYNC_EVERY_BYTES:
                    fdst.flush()
                    os.fsync(fdst.fileno())
                    since_last_sync = 0

                callback(src.name, copied, total, chunk)

            # Always sync the tail so the file is fully on disk/NAS before it
            # gets its final name and the source is unlinked.
            fdst.flush()
            os.fsync(fdst.fileno())
        try:
            shutil.copystat(str(src), str(tmp))
        except OSError as e:
            # SMB/NFS mounts often refuse timestamps/permissions — data is intact
            log.debug("copystat failed for %s: %s", dst, e)
        if dst.exists():
            raise FileExistsError(errno.EEXIST, "Destination exists", str(dst))
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    """Bytes that will actually be copied for this move (for the overall progress bar)."""
    src = Path(move["source_release"])
    if not src.exists():
        return 0
    if src.is_file():
        return src.stat().st_size
    if move.get("type") == "series" and move.get("video_files"):
        # Series moves copy only the video files, not .nfo/.srt/samples
        total = 0
        for vf in move["video_files"]:
            try:
                total += (src / vf).stat().st_size
            except OSError:
                pass
        return total
    total = 0
    for root, _, files in os.walk(src):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _subtitles_for(video: Path, single_video: bool) -> list[tuple[Path, str]]:
    """
    (subtitle file, target suffix) belonging to `video`: next to it or in a
    Subs/ folder, named after the video ("show.s01e01.de.srt" → ".de.srt").
    For a release with a single video, every subtitle there belongs to it
    ("Subs/English.srt" → ".English.srt").
    """
    stem = video.stem.lower()
    found = []
    for folder in (video.parent, *(video.parent / d for d in SUBTITLE_DIRS)):
        if not folder.is_dir():
            continue
        for f in sorted(folder.iterdir()):
            if not f.is_file() or f.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            if f.name.lower().startswith(stem + "."):
                found.append((f, f.name[len(stem):]))
            elif single_video:
                found.append((f, "." + f.name))
    return found


def _move_subtitles(video: Path, dst_video: Path, single_video: bool):
    """Move the subtitles of an episode next to it (best effort, never fails the move)."""
    for sub, suffix in _subtitles_for(video, single_video):
        target = dst_video.with_name(dst_video.stem + suffix)
        if target.exists():
            continue
        try:
            if not _try_rename(sub, target):
                _copy_with_progress(sub, target, lambda *a: None)
                sub.unlink(missing_ok=True)
            log.info("Moved subtitle: %s → %s", sub, target)
        except OSError as e:
            log.warning("Could not move subtitle %s: %s", sub, e)


def _cleanup_after_series_move(source_top: Path, source_release: Path, config: dict):
    """
    Remove the source once all episodes are gone. What is left are extras
    that series folders don't keep (.nfo, samples, screenshots, unrelated subs).
    """
    if source_release.is_dir():
        if find_videos(source_release, config):
            return
        log.info("Removing video-less source: %s", source_release)
        shutil.rmtree(str(source_release), ignore_errors=True)
    if source_top != source_release and source_top.exists():
        _remove_if_empty(source_top)


def _episode_file_name(rel_path: str) -> str:
    """
    Target file name of an episode. Releases with one folder per episode
    sometimes use the same generic file name everywhere ("Show.S01E01/show.mkv")
    — then the folder name carries the episode and becomes the file name.
    """
    rel = Path(rel_path)
    if _has_episode(rel.stem) or not _has_episode(rel.parent.name):
        return rel.name
    return rel.parent.name + rel.suffix


def _has_episode(name: str) -> bool:
    return bool(RE_SEASON_EP.search(name) or RE_SxE.search(name))


def _stat_key(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def _snapshot(src: Path) -> dict[Path, tuple[int, int]]:
    """{file: (size, mtime)} of a release (a single file or every file below a folder)."""
    if src.is_file():
        return {src: _stat_key(src)}
    snap = {}
    for root, _, fnames in os.walk(src):
        for fname in fnames:
            f = Path(root) / fname
            snap[f] = _stat_key(f)
    return snap


def _remove_empty_dirs(path: Path):
    """rmdir every empty folder below and including `path` — never deletes files."""
    for root, _, _ in os.walk(path, topdown=False):
        try:
            os.rmdir(root)
        except OSError:
            pass


def _existing_ancestor(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _same_device(src: Path, dest: Path) -> bool:
    try:
        return os.stat(src).st_dev == os.stat(_existing_ancestor(dest)).st_dev
    except OSError:
        return False


def _try_rename(src: Path, dst: Path) -> bool:
    """
    Move instantly via rename() when source and target share a filesystem.
    Returns False (→ caller copies) for different filesystems — including two
    separate Docker bind mounts of the same disk, where rename() fails with EXDEV.
    """
    if not _same_device(src, dst) or dst.exists():
        return False
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            log.warning("rename %s → %s failed (%s), falling back to copy", src, dst, e)
        return False
    log.info("Renamed (same filesystem): %s → %s", src, dst)
    return True


def _check_free_space(dest_dir: Path, needed: int) -> dict | None:
    """Return an error if the target filesystem cannot hold `needed` bytes."""
    try:
        free = shutil.disk_usage(_existing_ancestor(dest_dir)).free
    except OSError:
        return None  # can't tell (e.g. exotic mount) — let the copy decide
    if needed > free:
        need_gb, free_gb = f"{needed / 1024**3:.1f}", f"{free / 1024**3:.1f}"
        return _err("mover.no_space", f"Not enough free space on target: need {need_gb} GB, {free_gb} GB free",
                    need=need_gb, free=free_gb)
    return None


def _remove_if_empty(path: Path):
    try:
        if not any(f.is_file() for f in path.rglob("*")):
            shutil.rmtree(str(path), ignore_errors=True)
    except Exception as e:
        log.warning("Could not clean up %s: %s", path, e)
