"""Content-addressed store: a blob's name is its sha256.

Agents pass hashes instead of content (fewer tokens, no version drift).
Reads re-hash the blob, so silent corruption is detected.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ..util import canonical_json, sha256_hex

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CorruptBlob(Exception):
    pass


class CAS:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, h: str) -> Path:
        if not _HEX64.match(h):
            raise ValueError(f"not a sha256 hex digest: {h!r}")
        return self.root / h[:2] / h[2:]

    def put_bytes(self, data: bytes) -> str:
        h = sha256_hex(data)
        path = self._path(h)
        if path.exists():
            return h
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return h

    def put_text(self, text: str) -> str:
        return self.put_bytes(text.encode("utf-8"))

    def put_json(self, obj: Any) -> str:
        return self.put_text(canonical_json(obj))

    def exists(self, h: str) -> bool:
        return self._path(h).exists()

    def get_bytes(self, h: str) -> bytes:
        data = self._path(h).read_bytes()
        if sha256_hex(data) != h:
            raise CorruptBlob(h)
        return data

    def get_text(self, h: str) -> str:
        return self.get_bytes(h).decode("utf-8")

    def get_json(self, h: str) -> Any:
        return json.loads(self.get_text(h))
