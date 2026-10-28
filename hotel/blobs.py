"""Content-addressed blob store. The Reg only holds sha256 pointers."""

from __future__ import annotations

import hashlib
import os
import threading
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
            # One temp file per writer: two writers of the same content (threads, or two
            # processes opening one lobby) must not truncate or rename each other's file.
            tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_bytes(data)
            try:
                os.replace(tmp, p)
            except OSError:  # Windows: another writer's identical copy landed first and is open
                tmp.unlink(missing_ok=True)
                if not p.exists():
                    raise
        return h

    @staticmethod
    def hash_text(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

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

    def delete(self, h: str) -> bool:
        """Irreversibly remove content (forget). Reg rows keep the hash, so the
        hash chain stays intact; reads raise FileNotFoundError afterwards."""
        p = self._path(h)
        if p.exists():
            p.unlink()
            return True
        return False
