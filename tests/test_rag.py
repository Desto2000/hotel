"""RAG v2 (chunks, blobs, hybrid search, forget, backup), Concierge labels, next-edit suggestions."""

import json
import sqlite3

import numpy as np
import pytest

from hotel.backup import backup, restore
from hotel.chunking import chunk
from hotel.concierge import labels
from hotel.concierge.embed import normalize
from hotel.concierge.index import VectorIndex, rrf
from hotel.concierge.pointer import best_spans
from hotel.config import Paths
from hotel.core import Hotel, init_home
from hotel.gateway import make_pointer
from hotel.models import Annotation, AppendOp, Claim
from hotel.rag import Rag
from hotel.suggest import NextEdit, continuation

DOC = """# Exa guide

Intro paragraph about the API.

## Filters

### Dates

Use startPublishedDate to restrict results by publication time.

```python
exa.search("q", start_published_date="2026-01-01")
```

## Pricing

Searches cost money. """ + "Long sentence about billing details. " * 80


class Concepts:
    """Fake embedder: words map to concepts, so synonyms meet without sharing a word."""
    name = "fake:concepts"
    CONCEPTS = [{"date", "dates", "published", "publication", "time", "when", "recency"},
                {"price", "pricing", "cost", "billing", "money", "pay"},
                {"filter", "filters", "restrict", "narrow"}]

    def __init__(self):
        self.calls = 0

    def embed(self, texts, kind):
        self.calls += len(texts)
        out = []
        for t in texts:
            words = set(t.lower().replace(".", " ").replace(",", " ").split())
            out.append([len(words & c) for c in self.CONCEPTS] + [0.01])
        return normalize(np.array(out, dtype=np.float32))


@pytest.fixture
def hotel(tmp_path):
    init_home(tmp_path / "home")
    h = Hotel(Paths(tmp_path / "home"))
    h.index = VectorIndex(h.paths.vectors_db, Concepts())
    h.rag.index = h.derived.index = h.index
    return h


def test_chunks_are_exact_spans_with_heading_paths():
    cs = chunk(DOC, target=300, hard=500)
    assert all(DOC[c.start:c.end].strip() == DOC[c.start:c.end] for c in cs)
    dates = next(c for c in cs if "startPublishedDate to" in DOC[c.start:c.end])
    assert dates.heading == "Exa guide > Filters > Dates"
    assert "start_published_date" in DOC[dates.start:dates.end]  # the fence stays with its section
    assert not any(DOC[c.start:c.end] == "## Filters" for c in cs)  # an empty heading is no chunk
    billing = [c for c in cs if c.heading == "Exa guide > Pricing"]
    assert len(billing) > 1 and all(c.end - c.start <= 500 for c in billing)


def test_hybrid_search_finds_synonyms_and_records_signals(hotel, tmp_path):
    f = tmp_path / "guide.md"
    f.write_text(DOC, encoding="utf-8")
    [(_, did, new)] = hotel.rag_import([f])
    assert new and hotel.rag.get(did)["blob"] and hotel.rag.text(did) == DOC
    assert hotel.rag_import([f])[0][1:] == (did, False)  # identical import is de-duplicated
    # no word in common with the Dates chunk; only the dense ranking can find it
    top = hotel.rag.search("when was it published", 3)
    assert "startPublishedDate" in top[0]["body"] and "dense" in top[0]["signals"]
    assert top[0]["ref"].startswith(f"RAG:{did}#")
    calls = hotel.index.embedder.calls
    hotel.rag.search("pricing", 3)
    assert hotel.index.embedder.calls == calls + 1  # chunks are cached; only the query is embedded


def test_rrf_rewards_agreement():
    fused = [i for i, _, _ in rrf({"a": ["x", "y", "z"], "b": ["y", "x", "w"]})]
    assert fused[:2] in (["x", "y"], ["y", "x"]) and fused.index("w") > fused.index("z") - 1


def test_v1_store_migrates_in_place_keeping_ids(tmp_path):
    db = tmp_path / "rag.sqlite"
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE docs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source TEXT, title TEXT,
                       text TEXT NOT NULL, created_at TEXT NOT NULL);
                       CREATE VIRTUAL TABLE docs_fts USING fts5(title, text);""")
    c.execute("INSERT INTO docs VALUES (7, 'promotion', 'lib', 'promoted L0001', 'Bananas are yellow.', 'x')")
    c.commit()
    c.close()
    from hotel.blobs import BlobStore
    r = Rag(db, BlobStore(tmp_path / "blobs"))
    assert r.text(7) == "Bananas are yellow." and r.exists("RAG:7#0")
    assert r.search("bananas")[0]["ref"] == "RAG:7#0"


def test_rag_refs_are_checked_and_forget_removes_content(hotel, tmp_path):
    f = tmp_path / "n.md"
    f.write_text("Exa supports a startPublishedDate filter on search.\n", encoding="utf-8")
    did = hotel.rag_import([f])[0][1]
    b = hotel.apply([AppendOp(op="append", room="r", type="bounty", body="date filter?", refs=[], tags=[])]).created[0]
    e, _ = hotel.gateway.call("search", actor="user", query="startPublishedDate", sources=["rag"])
    text = hotel.blobs.text(e.text_hash)
    ptr = make_pointer(text, e.text_hash, "startPublishedDate filter")

    def ann(ref):
        return Annotation(target=e.id, caused="answers", caused_refs=[b], claims=[Claim(
            text="date filter", relation="supports", lobby_refs=[b, ref], pointer=ptr)])
    from hotel.gateway import AnnotationRejected
    with pytest.raises(AnnotationRejected, match="RAG:99"):
        hotel.gateway.annotate(ann("RAG:99"))
    hotel.gateway.annotate(ann(f"RAG:{did}#0"))

    labels.export(hotel)
    blob = hotel.rag.get(did)["blob"]
    info = hotel.forget_rag(did, reason="private")
    assert info["deleted"] and not hotel.blobs.exists(blob)
    assert hotel.rag.search("startPublishedDate") == [] and hotel.rag.exists(f"RAG:{did}#0")
    assert not hotel.paths.labels.exists()  # exported labels held copies of the content


def test_backup_carries_rag_blobs(hotel, tmp_path):
    f = tmp_path / "n.md"
    f.write_text("# Notes\n\nOnly in RAG, never in the Reg.\n", encoding="utf-8")
    did = hotel.rag_import([f])[0][1]
    backup(hotel.paths, tmp_path / "bk")
    res = restore(tmp_path / "bk", tmp_path / "restored")
    assert res["problems"] == []
    h2 = Hotel(Paths(tmp_path / "restored"))
    assert "Only in RAG" in h2.rag.text(did)


def test_labels_mark_quoted_hits_and_pointer_spans(hotel, tmp_path):
    for name, body in [("a.md", "Bananas are yellow fruit.\n"), ("b.md", "Exa supports a date filter.\n")]:
        (tmp_path / name).write_text(body, encoding="utf-8")
    hotel.rag_import([tmp_path / "a.md", tmp_path / "b.md"])
    e, _ = hotel.gateway.call("search", actor="user", query="exa date filter bananas", sources=["rag"])
    text = hotel.blobs.text(e.text_hash)
    ptr = make_pointer(text, e.text_hash, "Exa supports a date filter.")
    hotel.gateway.annotate(Annotation(target=e.id, caused="noted", claims=[Claim(
        text="Exa filters by date", relation="new", lobby_refs=[], pointer=ptr)]))
    lab = labels.mine(hotel)
    [p] = lab.pointer
    assert text[p["start"]:p["end"]] == "Exa supports a date filter."
    quoted = {r["id"]: r["quoted"] for r in lab.relevance}
    assert quoted == {"RAG:2#0": True, "RAG:1#0": False}
    q = next(r for r in lab.relevance if r["quoted"])
    assert q["signals"] and "bm25" in q["signals"]
    stats = labels.retrieval_stats(lab)
    assert stats["useful"] == 1 and stats["local_mrr"]["bm25"] > 0


def test_pointer_suggests_a_real_sentence():
    text = "QUERY: q\n\nExa has many features. Results can be filtered by publication date. Pricing is per search."
    [(s, e, _)] = best_spans("you can restrict results by published date", text, None, 1)
    assert text[s:e] == "Results can be filtered by publication date."


def test_continuation_only_extends_the_draft():
    assert continuation("Exa can filter", "```\nExa can filter by date<|cursor|>\n```") == "Exa can filter by date"
    assert continuation("Exa can filter", "```\nExa filters by date\n```") is None  # a rewrite, not ghost text
    assert continuation("Exa can filter", "```\nExa can filter\n```") is None


def test_next_edit_uses_local_context_and_logs_outcome(hotel):
    hotel.apply([AppendOp(op="append", room="exa", type="idea", body="Exa startPublishedDate narrows by time",
                          refs=[], tags=[])])
    bodies = []

    def transport(body):
        bodies.append(body)
        return json.dumps({"choices": [{"message": {"content": "```\nExa date filter is startPublishedDate\n```"}}],
                           "usage": {"prompt_tokens": 50, "completion_tokens": 9}}).encode()

    ne = NextEdit(hotel, lambda: "exa", transport=transport)
    sugg, rid = ne.request("Exa date filter is")
    assert sugg == "Exa date filter is startPublishedDate"
    prompt = bodies[0]["messages"][0]["content"]
    assert "lobby/exa/L0001" in prompt and "<|code_to_edit|>\nExa date filter is<|cursor|>" in prompt
    assert hotel.reg.get(rid).kind == "model" and hotel.reg.get(rid).payload["tokens_out"] == 9
    from hotel.suggest import Shown
    ne.shown = Shown("Exa date filter is", sugg, rid)
    assert ne.submitted("Exa date filter is startPublishedDate") == "accepted"
    [row] = [r for r in hotel.reg.list(kind="internal") if r.tool == "suggest.outcome"]
    assert row.payload["suggestion"] == rid and "startPublished" not in json.dumps(row.payload)
    assert labels.mine(hotel).suggestion[0]["outcome"] == "accepted"


def test_suggester_is_silent_without_optout(hotel):
    import asyncio
    ne = NextEdit(hotel, lambda: "inbox", debounce=0)
    assert asyncio.run(ne.get_suggestion("a long enough draft here")) is None
    assert "opt-out" in ne.error and not hotel.reg.list(kind="model")
