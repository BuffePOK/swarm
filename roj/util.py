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
