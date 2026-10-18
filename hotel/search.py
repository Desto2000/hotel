"""Search providers and the unified search interface.

Every result is normalized to: source, id, title, url, text, retrieved_at,
flag (external | own | secondhand). The canonical text rendering IS the blob
the model sees and that annotation pointers index into — so "what the model
read" and "what is stored" are the same bytes.

Patterns from Inception's search-agent cookbook: Exa `type: auto` + highlights,
retry with exponential backoff on 429/5xx, error text kept, fan-out of several
queries per round, query de-duplication (Jaccard > 0.8 rejected).
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

import httpx

from . import keys
from .derived import Derived
from .gateway import ToolResult
from .rag import Rag
from .reg import utcnow


@dataclass
class Hit:
    source: str  # exa | parallel | rag | lobby
    id: str
    title: str
    url: str | None
    text: str  # included verbatim in the blob
    retrieved_at: str
    flag: str  # external | own | secondhand
    published: str | None = None
    query: str | None = None

    @property
    def snippet(self) -> str:
        s = " ".join(self.text.split())
        return s[:280] + ("…" if len(s) > 280 else "")


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")


def clean_text(s: str) -> str:
    """Context hygiene: drop markdown images, render [text](url) as text.
    URLs inside prose cost tokens and break quoting; the hit's own url is kept."""
    return _MD_LINK.sub(r"\1", _MD_IMAGE.sub("", s))


def render_hits(query: str, hits: list[Hit], skipped: list[str] | None = None) -> str:
    out = [f"QUERY: {query}", f"RETRIEVED: {utcnow()}", ""]
    for n, h in enumerate(hits, 1):
        out.append(f"### [{n}] {h.source}:{h.id} ({h.flag}) {h.title}")
        if h.url:
            out.append(f"url: {h.url}")
        if h.published:
            out.append(f"published: {h.published}")
        if h.query and h.query != query:
            out.append(f"query: {h.query}")
        out += ["", clean_text(h.text).strip(), ""]
    if not hits:
        out.append("(no results)")
    if skipped:
        out.append("already retrieved earlier, omitted: " + ", ".join(skipped))
    return "\n".join(out) + "\n"


def _check(r: httpx.Response) -> httpx.Response:
    if r.is_error:
        raise RuntimeError(f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}")
    return r


def _post_retry(url: str, body: dict, headers: dict, timeout: float = 45) -> bytes:
    for attempt in range(5):
        r = httpx.post(url, json=body, headers=headers, timeout=timeout)
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 4:
            time.sleep(2 ** attempt)  # bursty fan-out hits 429s
            continue
        return _check(r).content
    raise RuntimeError("unreachable")


# ------------------------------------------------------------------ Exa
class ExaClient:
    BASE = "https://api.exa.ai"

    def __init__(self, api_key: str | None = None, max_chars: int = 4000):
        self.api_key = api_key
        self.max_chars = max_chars

    def _post(self, path: str, body: dict) -> bytes:
        key = self.api_key or keys.get("EXA_API_KEY")  # resolved per call: `hotel setup` can run while the TUI is open
        if not key:
            raise RuntimeError("EXA_API_KEY not set — run `hotel setup`")
        return _post_retry(self.BASE + path, body, {"x-api-key": key, "content-type": "application/json"})

    @staticmethod
    def _hits(raw: bytes, query: str | None = None) -> list[Hit]:
        now = utcnow()
        out = []
        for r in json.loads(raw).get("results", []):
            hl = r.get("highlights") or []
            text = "\n".join(f"- {h.strip()}" for h in hl) if hl else (r.get("text") or r.get("summary") or "")
            out.append(Hit(
                source="exa", id=str(r.get("id") or r.get("url")), title=(r.get("title") or "").strip(),
                url=r.get("url"), text=text, retrieved_at=now, flag="external",
                published=r.get("publishedDate"), query=query,
            ))
        return out

    def search(self, query: str, num_results: int = 5, mode: str = "text",
               highlight_chars: int = 2000) -> tuple[bytes, list[Hit]]:
        contents = ({"highlights": {"maxCharacters": highlight_chars}} if mode == "highlights"
                    else {"text": {"maxCharacters": self.max_chars}})
        raw = self._post("/search", {"query": query, "type": "auto", "numResults": num_results, "contents": contents})
        return raw, self._hits(raw, query)

    def fetch(self, urls: list[str]) -> tuple[bytes, list[Hit]]:
        raw = self._post("/contents", {"urls": urls, "text": {"maxCharacters": self.max_chars * 3}})
        return raw, self._hits(raw)


# ------------------------------------------------------------------ Parallel
class ParallelClient:
    """Parallel Search API (body shape from Inception's cookbook). Untested
    without PARALLEL_API_KEY."""

    URL = "https://api.parallel.ai/v1/search"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key

    def search_many(self, queries: list[str], objective: str, num_results: int = 5) -> tuple[bytes, list[Hit]]:
        key = self.api_key or keys.get("PARALLEL_API_KEY")
        if not key:
            raise RuntimeError("PARALLEL_API_KEY not set — run `hotel setup`")
        raw = _post_retry(self.URL, {
            "objective": objective, "search_queries": queries, "mode": "turbo",
            "advanced_settings": {"max_results": num_results * len(queries)},
        }, {"x-api-key": key, "Content-Type": "application/json"})
        now = utcnow()
        hits = [Hit("parallel", it.get("url") or "", it.get("title") or "", it.get("url"),
                    "\n".join(f"- {e.strip()}" for e in (it.get("excerpts") or [])), now, "external",
                    it.get("publish_date"), " | ".join(queries))
                for it in json.loads(raw).get("results", [])]
        return raw, hits


# ------------------------------------------------------------------ query hygiene
def clean_queries(raw: list, seen: list[set[str]], fanout: int) -> tuple[list[str], list[str]]:
    """Drop fragments and near-duplicates of queries already run. Returns (kept, rejected)."""
    kept, rejected = [], []
    for item in raw:
        q = " ".join(str(item).split())
        words = set(re.findall(r"[a-z0-9]+", q.lower()))
        if len(q) < 12 or len(words) < 3:
            rejected.append(f"{q!r} (fragment, not a query)")
            continue
        if any(len(words & prev) / len(words | prev) > 0.8 for prev in seen):
            rejected.append(f"{q!r} (near-duplicate of an earlier query)")
            continue
        if len(kept) >= fanout:
            rejected.append(f"{q!r} (over fan-out limit {fanout})")
            continue
        seen.append(words)
        kept.append(q)
    return kept, rejected


# ------------------------------------------------------------------ tool fns
def make_tools(exa: ExaClient, rag: Rag, derived: Derived, num_results: int = 5,
               parallel: ParallelClient | None = None, backend: str = "exa",
               agent_num_results: int = 3, agent_highlight_chars: int = 700):
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

    def web_many(queries: list[str], objective: str, n: int) -> tuple[dict, list[Hit]]:
        if backend == "parallel" and parallel is not None:
            raw, hits = parallel.search_many(queries, objective, n)
            return {"parallel": raw.decode("utf-8")}, hits
        with ThreadPoolExecutor(max_workers=max(1, len(queries))) as ex:
            results = list(ex.map(lambda q: exa.search(q, n, mode="highlights", highlight_chars=agent_highlight_chars), queries))
        raws = {q: r.decode("utf-8") for q, (r, _) in zip(queries, results)}
        return raws, [h for _, hs in results for h in hs]

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

    def agent_search(queries: list[str], objective: str, skip_urls: list[str] | None = None) -> ToolResult:
        """Agent fan-out: several web queries + RAG, one retrieval, one annotation.
        URLs already retrieved in this run are omitted to save context."""
        skip = set(skip_urls or [])
        raws, web = web_many(queries, objective, agent_num_results)
        rag_h = [h for q in queries for h in rag_hits(q)][:agent_num_results]
        hits, skipped, seen = [], [], set()
        for h in rag_h + web:
            key = h.url or h.id
            if key in skip:
                skipped.append(key)
                continue
            if key in seen:
                continue
            seen.add(key)
            hits.append(h)
        raws["rag"] = json.dumps([asdict(h) for h in rag_h], ensure_ascii=False)
        text = render_hits(" | ".join(queries), hits, sorted(set(skipped)))
        return ToolResult(json.dumps(raws, ensure_ascii=False).encode(), text, hits,
                          f"{'parallel' if backend == 'parallel' and parallel else 'exa'}+rag")

    return {"exa.search": exa_search, "exa.fetch": exa_fetch, "lobby.search": lobby_search,
            "search": unified, "agent.search": agent_search}
