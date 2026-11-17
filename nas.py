"""
Guards against hanging NAS mounts.

A dead SMB/NFS mount does not fail — every stat()/open() on it simply blocks,
and Python cannot interrupt a blocked system call. So instead of a timeout
around each call there are two guards:

- `unresponsive_dirs()` probes the configured folders in a helper thread
  before a scan or move starts and gives up after `nas_timeout` seconds.
- `StallWatch` notices when a running scan/move stops making progress, logs
  it once and reports it to the UI.

A thread blocked on a dead mount stays blocked until the mount comes back
(or the service restarts) — these guards make that visible, they cannot undo it.
"""

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable

from config import DIR_KEYS

log = logging.getLogger(__name__)

# path → (probe thread, start time); a probe hanging on a dead mount is reused
# instead of piling up a new blocked thread with every request
_probes: dict[str, tuple[threading.Thread, float]] = {}
_probes_lock = threading.Lock()


def nas_timeout(config: dict) -> float:
    """Configured `nas_timeout` in seconds; 0 = guards off."""
    try:
        return max(0.0, float(config.get("nas_timeout") or 0))
    except (TypeError, ValueError):
        return 0.0


def _probe(path: str):
    try:
        os.stat(path)
        with os.scandir(path) as entries:
            next(entries, None)
    except OSError:
        pass   # missing/unreadable folders are reported elsewhere — only hanging counts here


def unresponsive_dirs(config: dict, keys: Iterable[str] = DIR_KEYS) -> list[str]:
    """
    Configured folders (of `keys`) that do not answer a stat + directory read
    within `nas_timeout` seconds. Waits at most that long in total.
    """
    timeout = nas_timeout(config)
    if timeout <= 0:
        return []
    probes = []
    with _probes_lock:
        for key in keys:
            path = config[key]
            probe = _probes.get(path)
            if probe is None or not probe[0].is_alive():
                thread = threading.Thread(target=_probe, args=(path,), daemon=True, name=f"nas-probe {path}")
                probe = _probes[path] = (thread, time.monotonic())
                thread.start()
            probes.append((path, *probe))
    stuck = []
    for path, thread, started in probes:
        thread.join(max(0.0, started + timeout - time.monotonic()))
        if thread.is_alive():
            stuck.append(path)
    if stuck:
        log.error("NAS not responding: no answer from %s within %ss", ", ".join(stuck), timeout)
    return stuck


class StallWatch:
    """
    Background check for a running operation: if `touch()` is not called for
    `timeout` seconds, log a warning (once per stall) and call
    `on_stall(seconds)`; `on_stall(0)` when progress resumes.
    """

    def __init__(self, what: str, timeout: float, on_stall: Callable[[int], None], interval: float = 1.0):
        self._what = what
        self._timeout = timeout
        self._on_stall = on_stall
        self._interval = interval
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def touch(self):
        self._last = time.monotonic()

    def __enter__(self):
        if self._timeout > 0:
            self._thread = threading.Thread(target=self._run, daemon=True, name=f"stall-watch {self._what}")
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join()

    def _run(self):
        stalled = False
        while not self._stop.wait(self._interval):
            idle = time.monotonic() - self._last
            if idle >= self._timeout:
                if not stalled:
                    log.warning("%s: no progress for %ds — NAS not responding?", self._what, idle)
                stalled = True
                self._on_stall(max(1, round(idle)))   # 0 means "resumed"
            elif stalled:
                log.info("%s: progress resumed", self._what)
                stalled = False
                self._on_stall(0)
        if stalled:
            self._on_stall(0)
