"""Search providers and the unified search interface.

Every result is normalized to: source, id, title, url, snippet, retrieved_at,
flag (external | own | secondhand). Unified search over Exa + RAG + lobby is a
single gated retrieval whose canonical text blob lists every hit, so one
annotation covers one search.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

import httpx

from .derived import Derived
from .gateway import ToolResult
from .rag import Rag
from .reg import utcnow


@dataclass
class Hit:
    source: str  # exa | rag | lobby | parallel
    id: str
    title: str
    url: str | None
    text: str  # full text included in the blob
    retrieved_at: str
    flag: str  # external | own | secondhand
    published: str | None = None

    @property
    def snippet(self) -> str:
        s = " ".join(self.text.split())
        return s[:280] + ("…" if len(s) > 280 else "")


def render_hits(query: str, hits: list[Hit]) -> str:
    out = [f"QUERY: {query}", f"RETRIEVED: {utcnow()}", ""]
    for n, h in enumerate(hits, 1):
        out.append(f"### [{n}] {h.source}:{h.id} ({h.flag}) {h.title}")
        if h.url:
            out.append(f"url: {h.url}")
        if h.published:
            out.append(f"published: {h.published}")
        out += ["", h.text.strip(), ""]
    if not hits:
        out.append("(no results)")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ Exa
class ExaClient:
    BASE = "https://api.exa.ai"

    def __init__(self, api_key: str | None = None, max_chars: int = 4000):
        self.api_key = api_key or os.environ.get("EXA_API_KEY") or os.environ.get("MCP_EXA_API_KEY")
        self.max_chars = max_chars

    def _post(self, path: str, body: dict) -> bytes:
        if not self.api_key:
            raise RuntimeError("EXA_API_KEY not set")
        r = httpx.post(
            self.BASE + path, json=body, timeout=45,
            headers={"x-api-key": self.api_key, "content-type": "application/json"},
        )
        r.raise_for_status()
        return r.content

    def _hits(self, raw: bytes) -> list[Hit]:
        now = utcnow()
        out = []
        for r in json.loads(raw).get("results", []):
            out.append(Hit(
                source="exa", id=str(r.get("id") or r.get("url")), title=(r.get("title") or "").strip(),
                url=r.get("url"), text=r.get("text") or r.get("summary") or "", retrieved_at=now,
                flag="external", published=r.get("publishedDate"),
            ))
        return out

    def search(self, query: str, num_results: int = 5) -> tuple[bytes, list[Hit]]:
        raw = self._post("/search", {
            "query": query, "numResults": num_results,
            "contents": {"text": {"maxCharacters": self.max_chars}},
        })
        return raw, self._hits(raw)

    def fetch(self, urls: list[str]) -> tuple[bytes, list[Hit]]:
        raw = self._post("/contents", {"urls": urls, "text": {"maxCharacters": self.max_chars * 3}})
        return raw, self._hits(raw)


# ------------------------------------------------------------------ tool fns
def make_tools(exa: ExaClient, rag: Rag, derived: Derived, num_results: int = 5):
    def exa_search(query: str) -> ToolResult:
        raw, hits = exa.search(query, num_results)
        return ToolResult(raw, render_hits(query, hits), hits, "exa")

    def exa_fetch(url: str) -> ToolResult:
        raw, hits = exa.fetch([url])
        return ToolResult(raw, render_hits(f"fetch {url}", hits), hits, "exa")

    def rag_hits(query: str) -> list[Hit]:
        now = utcnow()
        return [Hit("rag", f"RAG:{d['id']}", d["title"] or "", d["source"], d["text"], now, "external")
                for d in rag.search(query, 5)]

    def lobby_hits(query: str) -> list[Hit]:
        now = utcnow()
        return [Hit("lobby", r["eid"], f"{r['type']} in {r['room']} [{r['status']}]", None, r["body"], now, "own")
                for r in derived.search(query, 8)]

    def lobby_search(query: str) -> ToolResult:
        hits = lobby_hits(query)
        raw = json.dumps([asdict(h) for h in hits], ensure_ascii=False).encode()
        return ToolResult(raw, render_hits(query, hits), hits, "lobby")

    def unified(query: str, sources: list[str] | None = None) -> ToolResult:
        sources = sources or ["exa", "rag", "lobby"]
        hits: list[Hit] = []
        raws: dict[str, str] = {}
        used = []
        if "lobby" in sources:
            lh = lobby_hits(query)
            hits += lh
            raws["lobby"] = json.dumps([asdict(h) for h in lh], ensure_ascii=False)
            used.append("lobby")
        if "rag" in sources:
            rh = rag_hits(query)
            hits += rh
            raws["rag"] = json.dumps([asdict(h) for h in rh], ensure_ascii=False)
            used.append("rag")
        if "exa" in sources:
            raw, eh = exa.search(query, num_results)
            hits += eh
            raws["exa"] = raw.decode("utf-8")
            used.append("exa")
        return ToolResult(json.dumps(raws, ensure_ascii=False).encode(), render_hits(query, hits), hits, "+".join(used))

    return {"exa.search": exa_search, "exa.fetch": exa_fetch, "lobby.search": lobby_search, "search": unified}
