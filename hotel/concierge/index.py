"""Vector index: content-addressed embeddings + exact search + rank fusion.

Vectors are keyed by sha256 of the exact text that was embedded, inside the
embedder's namespace — the same idea as the blob store. Identical text is
embedded once, a lobby rebuild re-embeds nothing, and a model change starts a
fresh namespace instead of mixing vector spaces. The table is a cache
(.hotel/vectors.sqlite): it is not backed up and can be deleted at any time.

Search is exact (one matrix-vector product). At personal scale (1e4-1e5
chunks) that takes milliseconds and has perfect recall, so no ANN index.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

from .embed import Embedder

RETRY_AFTER_S = 300  # an embedder that failed (offline first download) is retried after this


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rrf(rankings: dict[str, list[str]], k: int = 60) -> list[tuple[str, float, dict[str, int]]]:
    """Reciprocal rank fusion (Cormack, Clarke & Büttcher 2009): score = Σ 1/(k + rank).
    Uses ranks only, so bm25 and cosine scores never need calibrating against each other.
    Returns (id, score, {ranking name: 1-based rank}) best first."""
    score: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for name, ids in rankings.items():
        for r, i in enumerate(ids, 1):
            score[i] = score.get(i, 0.0) + 1.0 / (k + r)
            ranks.setdefault(i, {})[name] = r
    return sorted(((i, s, ranks[i]) for i, s in score.items()), key=lambda t: -t[1])


class VectorIndex:
    def __init__(self, db_path: Path, embedder: Embedder):
        self.embedder = embedder
        self.conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS vec (
            model TEXT NOT NULL, key TEXT NOT NULL, v BLOB NOT NULL, PRIMARY KEY (model, key)) WITHOUT ROWID""")
        self._lock = threading.RLock()
        self._mem: dict[str, np.ndarray] | None = None
        self._queries: OrderedDict[str, np.ndarray] = OrderedDict()
        self._mats: OrderedDict[str, np.ndarray] = OrderedDict()
        self.error: str | None = None
        self._failed_at = 0.0

    @property
    def name(self) -> str:
        return self.embedder.name

    def _vectors(self) -> dict[str, np.ndarray]:
        if self._mem is None:
            rows = self.conn.execute("SELECT key, v FROM vec WHERE model=?", (self.name,))
            self._mem = {k: np.frombuffer(v, dtype=np.float16).astype(np.float32) for k, v in rows}
        return self._mem

    def __len__(self) -> int:
        with self._lock:
            return len(self._vectors())

    def ensure(self, texts: list[str], batch: int = 32, progress=None) -> list[str]:
        """Embed the texts not cached yet; return their keys (same order)."""
        keys = [text_key(t) for t in texts]
        with self._lock:
            mem = self._vectors()
            todo = list({k: t for k, t in zip(keys, texts) if k not in mem}.items())
        for i in range(0, len(todo), batch):
            part = todo[i:i + batch]
            vecs = self.embedder.embed([t for _, t in part], "passage")
            with self._lock:
                self.conn.execute("BEGIN")
                self.conn.executemany("INSERT OR IGNORE INTO vec(model, key, v) VALUES (?,?,?)",
                                      [(self.name, k, v.astype(np.float16).tobytes()) for (k, _), v in zip(part, vecs)])
                self.conn.execute("COMMIT")
                for (k, _), v in zip(part, vecs):
                    mem[k] = v.astype(np.float16).astype(np.float32)  # what is stored is what is searched
            if progress:
                progress(min(i + batch, len(todo)), len(todo))
        return keys

    def query(self, text: str) -> np.ndarray:
        with self._lock:
            if text in self._queries:
                self._queries.move_to_end(text)
                return self._queries[text]
        v = self.embedder.embed([text], "query")[0]
        with self._lock:
            self._queries[text] = v
            while len(self._queries) > 256:
                self._queries.popitem(last=False)
        return v

    def _matrix(self, keys: list[str]) -> np.ndarray:
        sig = hashlib.sha1("".join(keys).encode()).hexdigest()
        with self._lock:
            if sig not in self._mats:
                mem = self._vectors()
                self._mats[sig] = np.stack([mem[k] for k in keys])
                while len(self._mats) > 4:
                    self._mats.popitem(last=False)
            return self._mats[sig]

    def rank(self, query: str, texts: list[str], k: int = 50) -> list[tuple[int, float]] | None:
        """Indices into `texts` by cosine similarity to `query`, best first.
        None when the embedder is unavailable: callers fall back to lexical search."""
        if not texts:
            return []
        if self.error and time.monotonic() - self._failed_at < RETRY_AFTER_S:
            return None
        if not getattr(self.embedder, "ready", True):
            self.warm_in_background()  # the first download takes minutes: never block a search on it
            return None
        try:
            keys = self.ensure(texts)
            q = self.query(query)
        except Exception as ex:  # e.g. the model cannot be downloaded offline
            self.error, self._failed_at = f"{type(ex).__name__}: {ex}"[:300], time.monotonic()
            return None
        self.error = None
        s = self._matrix(keys) @ q
        top = np.argsort(-s)[:k]
        return [(int(i), float(s[i])) for i in top]

    def warm_in_background(self) -> None:
        with self._lock:
            if getattr(self, "_warming", None) and self._warming.is_alive():
                return

            def run():
                try:
                    self.embedder.warm()
                except Exception as ex:
                    self.error, self._failed_at = f"{type(ex).__name__}: {ex}"[:300], time.monotonic()

            self._warming = threading.Thread(target=run, name="hotel-embedder-warmup", daemon=True)
            self._warming.start()

    def drop(self, texts: list[str]) -> int:
        """Delete the vectors of these texts (forget: embeddings can be inverted to text)."""
        keys = [text_key(t) for t in texts]
        with self._lock:
            mem = self._vectors()
            self.conn.executemany("DELETE FROM vec WHERE model=? AND key=?", [(self.name, k) for k in keys])
            for k in keys:
                mem.pop(k, None)
            self._mats.clear()
        return len(keys)
