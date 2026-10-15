"""Gunicorn settings for production (used by folder-mover.service)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config  # noqa: E402

_cfg = load_config()

bind = f"{_cfg.get('host', '0.0.0.0')}:{_cfg.get('port', 8080)}"

# Scan/move state lives in process memory → there must be exactly ONE worker.
# Threads serve the parallel SSE streams (scan + move progress).
workers = 1
worker_class = "gthread"
threads = 16
timeout = 120
graceful_timeout = 30
