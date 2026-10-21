"""Zero-shot quote pointer: the source sentence that best supports a claim.

The future pointer head extracts a span from the retrieval directly. Until it
is trained, this finds the closest sentence by embedding similarity (falling
back to word overlap without an embedder). It never invents text: it returns
spans of the stored retrieval, so whatever it proposes passes the gate's
quote check verbatim. Used to turn a rejected model quote into a concrete
"did you mean" correction.
"""

from __future__ import annotations

import re

import numpy as np

from .embed import Embedder

SENT = re.compile(r"[^\n.!?]+(?:[.!?]+|$)", re.M)
WORD = re.compile(r"\w+", re.UNICODE)


def sentences(text: str, min_chars: int = 25, max_chars: int = 400) -> list[tuple[int, int]]:
    """Sentence spans [start, end) of text, whitespace-trimmed; headers and fragments skipped."""
    out = []
    for m in SENT.finditer(text):
        s, e = m.start(), m.end()
        while s < e and text[s] in " \t-*•|>#":
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if min_chars <= e - s <= max_chars and not text[s:e].startswith(("url:", "published:", "QUERY:", "RETRIEVED:")):
            out.append((s, e))
    return out


def _overlap(a: str, b: str) -> float:
    wa, wb = set(WORD.findall(a.lower())), set(WORD.findall(b.lower()))
    return len(wa & wb) / (len(wa) or 1)


def best_spans(claim: str, text: str, embedder: Embedder | None = None, k: int = 2) -> list[tuple[int, int, float]]:
    """Retrieval text is embedded in memory only, never cached: `forget` must not leave vectors behind."""
    spans = sentences(text)
    if not spans:
        return []
    if embedder is not None and getattr(embedder, "ready", True):
        try:
            m = embedder.embed([text[s:e] for s, e in spans], "passage")
            q = embedder.embed([claim], "query")[0]
            sc = m @ q
            return [(spans[i][0], spans[i][1], float(sc[i])) for i in np.argsort(-sc)[:k]]
        except Exception:  # embedder unavailable: lexical fallback
            pass
    scored = sorted(((s, e, _overlap(claim, text[s:e])) for s, e in spans), key=lambda t: -t[2])
    return [t for t in scored[:k] if t[2] > 0]
