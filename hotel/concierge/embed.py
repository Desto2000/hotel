"""Embedders: texts -> L2-normalized float32 vectors.

The default is a pretrained multilingual model run locally through ONNX
(fastembed), so lobby text never leaves the machine for retrieval. Queries and
passages are embedded differently (models use task prefixes); `kind` selects.

Configure with `"embedder": "fastembed:<model>"` in .hotel/config.json, or
`HOTEL_EMBEDDER` (which wins; `none` turns semantic search off).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

Kind = Literal["query", "passage"]
MODEL_DIR = Path.home() / ".cache" / "hotel" / "models"  # outside the lobby and its backups


class Embedder(Protocol):
    name: str  # namespace of cached vectors: changing the model never mixes vector spaces

    def embed(self, texts: list[str], kind: Kind) -> np.ndarray: ...

    def warm(self) -> None: ...


def normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(n == 0, 1, n)


class FastEmbedder:
    """Runs the model in a child process (`python -m hotel.concierge.embed`).

    onnxruntime holds the GIL while it builds a session (~1 s for the default model) and
    while it embeds a batch, so in-process it froze the TUI even from a worker thread. The
    child also keeps download progress bars off the TUI's terminal.

    Protocol over the child's stdin/stdout: the child sends one JSON line when the model is
    loaded ({"ok": true} or {"error": ...}); then per request the parent sends a JSON line
    {"kind", "texts"} and gets back {"n", "d"} plus n*d float32 bytes, or {"error": ...}.
    """

    def __init__(self, model: str, cache_dir: Path = MODEL_DIR):
        self.model = model
        self.name = f"fastembed:{model}"
        self.cache_dir = cache_dir
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()  # one request on the pipe at a time

    @property
    def ready(self) -> bool:
        """Loaded, or on disk (loading from disk takes seconds; only a download is slow)."""
        if self._proc is not None:
            return True
        tail = self.model.split("/")[-1].lower()
        return self.cache_dir.is_dir() and any(tail in d.name.lower() for d in self.cache_dir.iterdir())

    def _start(self) -> subprocess.Popen:
        """The running child; starts it and waits for the model to load. Call with the lock held."""
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        p = subprocess.Popen([sys.executable, "-m", "hotel.concierge.embed", self.model, str(self.cache_dir)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._proc = p
        self._reply(p)  # first use downloads the model; concurrent callers wait on the lock
        return p

    def _reply(self, p: subprocess.Popen) -> dict:
        line = p.stdout.readline()
        msg = json.loads(line) if line else {"error": f"embedder process exited ({p.wait()})"}
        if "error" in msg:
            self._proc = None
            raise RuntimeError(msg["error"])
        return msg

    def warm(self) -> None:
        with self._lock:
            self._start()

    def embed(self, texts: list[str], kind: Kind) -> np.ndarray:
        with self._lock:
            p = self._start()
            try:
                p.stdin.write(json.dumps({"kind": kind, "texts": texts}).encode() + b"\n")
                p.stdin.flush()
            except OSError as ex:  # the child died between requests
                self._proc = None
                raise RuntimeError(f"embedder process is gone: {ex}") from ex
            head = self._reply(p)
            data = p.stdout.read(head["n"] * head["d"] * 4)
        return np.frombuffer(data, dtype=np.float32).reshape(head["n"], head["d"])


def _serve(model: str, cache_dir: str) -> None:
    """Child process: load the model, then answer embed requests until stdin closes."""
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C belongs to the parent, which ends us by closing stdin
    out = sys.stdout.buffer
    try:
        from fastembed import TextEmbedding
        m = TextEmbedding(model, cache_dir=cache_dir)
    except Exception as ex:
        out.write(json.dumps({"error": f"{type(ex).__name__}: {ex}"[:500]}).encode() + b"\n")
        return
    out.write(b'{"ok": true}\n')
    out.flush()
    for line in sys.stdin.buffer:
        req = json.loads(line)
        try:
            it = m.query_embed(req["texts"]) if req["kind"] == "query" else m.passage_embed(req["texts"])
            v = normalize(np.array(list(it)))
        except Exception as ex:
            out.write(json.dumps({"error": f"{type(ex).__name__}: {ex}"[:500]}).encode() + b"\n")
        else:
            out.write(json.dumps({"n": v.shape[0], "d": v.shape[1]}).encode() + b"\n" + v.tobytes())
        out.flush()


def make_embedder(cfg: dict) -> Embedder | None:
    spec = os.environ.get("HOTEL_EMBEDDER") or cfg.get("embedder")
    if not spec or spec == "none":
        return None
    backend, _, model = spec.partition(":")
    if backend != "fastembed" or not model:
        raise ValueError(f"unknown embedder {spec!r}; use 'fastembed:<model>' or 'none'")
    return FastEmbedder(model)


if __name__ == "__main__":
    _serve(sys.argv[1], sys.argv[2])
