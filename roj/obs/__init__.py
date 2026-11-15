"""Observability: task logs, process log, doctor, diagnostic bundles."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_configured: set[str] = set()


def setup_process_log(home: Path, level: int = logging.INFO) -> Path:
    """Rotating <home>/logs/roj.log (5 x 5 MB). Idempotent."""
    path = Path(home) / "logs" / "roj.log"
    if str(path) in _configured:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s pid=%(process)d | %(message)s"))
    root = logging.getLogger("roj")
    root.setLevel(level)
    root.addHandler(handler)
    _configured.add(str(path))
    return path
