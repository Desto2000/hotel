"""Embedders: texts -> L2-normalized float32 vectors.

The default is a pretrained multilingual model run locally through ONNX
(fastembed), so lobby text never leaves the machine for retrieval. Queries and
passages are embedded differently (models use task prefixes); `kind` selects.

Configure with `"embedder": "fastembed:<model>"` in .hotel/config.json, or
`HOTEL_EMBEDDER` (which wins; `none` turns semantic search off).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

Kind = Literal["query", "passage"]
MODEL_DIR = Path.home() / ".cache" / "hotel" / "models"  # outside the lobby and its backups


class Embedder(Protocol):
    name: str  # namespace of cached vectors: changing the model never mixes vector spaces

    def embed(self, texts: list[str], kind: Kind) -> np.ndarray: ...


def normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(n == 0, 1, n)


class FastEmbedder:
    def __init__(self, model: str, cache_dir: Path = MODEL_DIR):
        self.model = model
        self.name = f"fastembed:{model}"
        self.cache_dir = cache_dir
        self._m = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        """Loaded, or on disk (loading from disk takes seconds; only a download is slow)."""
        if self._m is not None:
            return True
        tail = self.model.split("/")[-1].lower()
        return self.cache_dir.is_dir() and any(tail in d.name.lower() for d in self.cache_dir.iterdir())

    def _load(self):
        with self._lock:  # first use downloads the model once; concurrent callers wait for it
            if self._m is None:
                from fastembed import TextEmbedding
                self._m = TextEmbedding(self.model, cache_dir=str(self.cache_dir))
            return self._m

    def warm(self) -> None:
        self._load()

    def embed(self, texts: list[str], kind: Kind) -> np.ndarray:
        m = self._load()
        it = m.query_embed(texts) if kind == "query" else m.passage_embed(texts)
        return normalize(np.array(list(it)))


def make_embedder(cfg: dict) -> Embedder | None:
    spec = os.environ.get("HOTEL_EMBEDDER") or cfg.get("embedder")
    if not spec or spec == "none":
        return None
    backend, _, model = spec.partition(":")
    if backend != "fastembed" or not model:
        raise ValueError(f"unknown embedder {spec!r}; use 'fastembed:<model>' or 'none'")
    return FastEmbedder(model)
