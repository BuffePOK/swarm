"""Small deterministic helpers shared by every layer."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

ZERO_HASH = "0" * 64


def canonical_json(obj: Any) -> str:
    """Byte-stable JSON: sorted keys, no whitespace, UTF-8 kept as is.

    Used for hashing, cache keys and prompt prefixes, so two equal objects
    always serialise to the same bytes.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def _default(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="milliseconds")


def day_key(tz_name: str, at: datetime | None = None) -> str:
    """Calendar day (YYYY-MM-DD) in the given IANA timezone."""
    at = at or utcnow()
    return at.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def estimate_tokens(text: str) -> int:
    """Pessimistic token estimate without a tokenizer.

    ~3 characters per token over-counts English (≈4) and is close for Polish,
    which is what a budget reservation wants: never under-reserve.
    """
    return max(1, math.ceil(len(text) / 3))


def truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def remove_tree(path, attempts: int = 5) -> bool:
    """Delete a directory tree; True if it is gone.

    On Windows git writes its object files read-only and a just-closed SQLite file or child
    process can hold a handle for a moment, so plain shutil.rmtree leaves the tree behind.
    Read-only flags are cleared and the removal is retried briefly.
    """
    import os
    import shutil
    import stat
    import sys
    import time
    from pathlib import Path

    p = Path(path)

    def clear_and_retry(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)
        except OSError:
            pass  # the next attempt will try again

    for i in range(attempts):
        if not p.exists():
            return True
        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=clear_and_retry)
        else:
            shutil.rmtree(p, onerror=clear_and_retry)
        if not p.exists():
            return True
        time.sleep(0.2 * (i + 1))
    return not p.exists()
