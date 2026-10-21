"""Labels: training and evaluation data mined from the Reg.

Every judgment the user (or a gated model) already makes is a label:

  pointer     an accepted annotation claim: (claim, source text, start, end).
              Supervision for a span-extraction head; valid by construction,
              because the gate re-verified each span against the stored blob.
  relevance   which hits of a retrieval were quoted (positive) and which were
              not (negative), with the local ranks they had (bm25/dense/rrf)
              when the hit came from the lobby or RAG. Retrieval eval data.
  pairs       (lobby entry text, quote that supported/contradicted it):
              positives for a personal embedding head.
  suggestion  Mercury Edit suggestions shown in the composer and whether they
              were accepted, edited or dismissed.
  review      drafts the user approved or rejected (with the proposing actor).

Forgotten content simply drops out: its blobs are gone, so it cannot be
mined. Exported files (.hotel/labels/) copy content, so `forget` deletes them;
every export records the Reg head it was built from (`manifest.json`), which
is also the identity a future checkpoint trained on it must carry.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field

from ..gateway import load_annotation

HIT_HEADER = re.compile(r"^### \[(\d+)\] (\S+?):(\S+) \((\w+)\)", re.M)


@dataclass
class Labels:
    pointer: list[dict] = field(default_factory=list)
    relevance: list[dict] = field(default_factory=list)
    pairs: list[dict] = field(default_factory=list)
    suggestion: list[dict] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    reg_head: int = 0

    def counts(self) -> dict[str, int]:
        return {k: len(getattr(self, k)) for k in ("pointer", "relevance", "pairs", "suggestion", "review")}


def hit_spans(text: str) -> list[dict]:
    """The hits of a rendered retrieval (search.render_hits) as character spans."""
    ms = list(HIT_HEADER.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append({"n": int(m.group(1)), "source": m.group(2), "id": m.group(3), "flag": m.group(4),
                    "start": m.start(), "end": end})
    return out


def _raw_signals(hotel, row) -> dict[str, dict]:
    """hit id -> local ranking signals, from the retrieval's raw blob (lobby / RAG hits only)."""
    try:
        raw = json.loads(hotel.blobs.get(row.result_hash))
    except Exception:
        return {}
    out = {}
    for part in (raw.values() if isinstance(raw, dict) else []):
        try:
            items = json.loads(part) if isinstance(part, str) else part
        except ValueError:
            continue
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("signals"):
                out[str(it.get("id"))] = it["signals"]
    return out


def mine(hotel) -> Labels:
    reg, blobs = hotel.reg, hotel.blobs
    lab = Labels()
    lob = hotel.lobby().entries
    for a in reg.list(kind="annotation", limit=10**9):
        lab.reg_head = max(lab.reg_head, a.id)
        if a.status != "ok":
            continue
        ann = load_annotation(a, blobs)
        r = reg.get(a.ref_id)
        if ann is None or r is None or not r.text_hash:
            continue
        try:
            text = blobs.text(r.text_hash)
        except FileNotFoundError:
            continue
        hits = hit_spans(text)
        quoted: set[int] = set()
        for c in ann["claims"]:
            p = c["pointer"]
            lab.pointer.append({"retrieval": r.id, "annotation": a.id, "actor": a.actor, "claim": c["text"],
                                "relation": c["relation"], "start": p["start"], "end": p["end"], "quote": p["quote"],
                                "text_blob": r.text_hash})
            for h in hits:
                if h["start"] <= p["start"] < h["end"]:
                    quoted.add(h["n"])
            if c["relation"] in ("supports", "contradicts"):
                for ref in c["lobby_refs"]:
                    if ref in lob:
                        lab.pairs.append({"anchor": ref, "anchor_text": lob[ref].text, "positive": p["quote"],
                                          "relation": c["relation"], "retrieval": r.id, "actor": a.actor})
        if hits:
            sig = _raw_signals(hotel, r)
            for h in hits:
                lab.relevance.append({"retrieval": r.id, "tool": r.tool, "n": h["n"], "source": h["source"],
                                      "id": h["id"], "quoted": h["n"] in quoted, "empty": not ann["claims"],
                                      "signals": sig.get(h["id"]), "actor": a.actor})
    for s in reg.list(kind="internal", limit=10**9):
        if s.tool == "suggest.outcome" and s.payload:
            lab.suggestion.append({"reg": s.id, **s.payload})
    reviews = {r.id: r for r in reg.list(kind="review", limit=10**9)}
    for d in reg.list(kind="decision", limit=10**9):
        rv = reviews.get(d.ref_id)
        if rv and d.payload:
            lab.review.append({"review": rv.id, "proposer": rv.actor, "op": rv.payload.get("op"),
                               "reason": rv.payload.get("reason"), "verdict": d.payload.get("verdict")})
    last = reg.list(limit=1)
    lab.reg_head = last[0].id if last else 0
    return lab


def retrieval_stats(lab: Labels) -> dict:
    """How good is retrieval, judged by what was actually quoted.
    hit@k: share of useful retrievals whose first quoted hit was in the top k; MRR likewise.
    Per local ranking (bm25 / dense / rrf): the same, over lobby/RAG hits that carried signals."""
    by_r: dict[int, list[dict]] = {}
    for x in lab.relevance:
        by_r.setdefault(x["retrieval"], []).append(x)
    useful = [rows for rows in by_r.values() if any(r["quoted"] for r in rows)]
    first = [min(r["n"] for r in rows if r["quoted"]) for rows in useful]
    out = {"retrievals": len(by_r), "useful": len(useful), "empty": sum(1 for rows in by_r.values()
                                                                         if rows[0]["empty"])}
    if first:
        out.update({"hit@1": round(sum(f <= 1 for f in first) / len(first), 3),
                    "hit@3": round(sum(f <= 3 for f in first) / len(first), 3),
                    "mrr": round(sum(1 / f for f in first) / len(first), 3)})
    local: dict[str, list[float]] = {}
    for x in lab.relevance:
        if x["quoted"] and x["signals"]:
            for name, rank in x["signals"].items():
                if name != "rrf":
                    local.setdefault(name, []).append(1 / rank)
    out["local_mrr"] = {k: round(sum(v) / len(v), 3) for k, v in local.items()}
    out["by_source"] = dict(Counter(x["source"] for x in lab.relevance if x["quoted"]))
    return out


def export(hotel, lab: Labels | None = None) -> dict:
    lab = lab or mine(hotel)
    d = hotel.paths.labels
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for name, rows in lab.counts().items():
        with open(d / f"{name}.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for row in getattr(lab, name):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = {"reg_head": lab.reg_head, "counts": lab.counts(), "stats": retrieval_stats(lab)}
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"dir": str(d), **manifest}


def invalidate(hotel) -> bool:
    """Called by forget: exported labels may hold copies of the forgotten content."""
    if hotel.paths.labels.exists():
        shutil.rmtree(hotel.paths.labels)
        return True
    return False
