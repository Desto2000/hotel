"""Content-addressed blob store. The Reg only holds sha256 pointers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, h: str) -> Path:
        return self.root / h[:2] / h[2:]

    def put(self, data: bytes) -> str:
        h = hashlib.sha256(data).hexdigest()
        p = self._path(h)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)
        return h

    def put_text(self, s: str) -> str:
        return self.put(s.encode("utf-8"))

    def get(self, h: str) -> bytes:
        data = self._path(h).read_bytes()
        if hashlib.sha256(data).hexdigest() != h:
            raise ValueError(f"blob {h} is corrupt")
        return data

    def text(self, h: str) -> str:
        return self.get(h).decode("utf-8")

    def exists(self, h: str) -> bool:
        return self._path(h).exists()
