"""RAG store: imported + promoted knowledge, chunked and hybrid-searched.

References are one-way: the lobby cites RAG:<n> (a document) or RAG:<n>#<k>
(its k-th chunk); RAG documents never cite the lobby. Promotion provenance
lives in the Reg (the lobby_edit row), not in the RAG document.

A document's text is a content-addressed blob, like every other piece of
content, so it can be forgotten and identical imports are de-duplicated.
Chunks are exact [start, end) spans of that text (see chunking.py), indexed
twice: FTS5 (bm25) and, when an embedder is configured, dense vectors. The
two rankings are fused with reciprocal rank fusion.

Schema history: v1 stored `text` inline in `docs` with one FTS row per whole
document; it is migrated in place on open (ids are kept, so RAG:<n> refs hold).
"""

from __future__ import annotations

import html.parser
import re
import sqlite3
import threading
from pathlib import Path

from .blobs import BlobStore
from .chunking import chunk
from .concierge.index import VectorIndex, rrf
from .derived import fts_query
from .reg import utcnow

VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,         -- import | promotion
    source TEXT,
    title TEXT,
    blob TEXT,                  -- sha256 of the text; NULL once forgotten
    chars INTEGER,
    created_at TEXT NOT NULL,
    forgotten_at TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    doc_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    heading TEXT NOT NULL,
    PRIMARY KEY (doc_id, idx)
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(doc_id UNINDEXED, idx UNINDEXED, title, heading, body);
"""
REF = re.compile(r"^RAG:(\d+)(?:#(\d+))?$")
CANDIDATES = 50  # per ranking, before fusion


def embed_text(c: dict) -> str:
    """What the embedder sees: the chunk with its document title and heading path."""
    head = " > ".join(x for x in (c["title"], c["heading"]) if x)
    return f"{head}\n\n{c['body']}" if head else c["body"]


class Rag:
    def __init__(self, db_path: Path, blobs: BlobStore, index: VectorIndex | None = None):
        self.blobs = blobs
        self.index = index
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._chunks: list[dict] | None = None  # in-memory corpus for dense search
        with self._lock:
            if self.conn.execute("PRAGMA user_version").fetchone()[0] < VERSION:
                self._migrate()

    # ------------------------------------------------------------- schema
    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(docs)")}
        old = []
        if "text" in cols:  # v1
            old = [dict(r) for r in self.conn.execute("SELECT * FROM docs ORDER BY id")]
            self.conn.executescript("DROP TABLE IF EXISTS docs_fts; DROP TABLE docs;")
        self.conn.executescript(SCHEMA)
        for d in old:
            self._insert(d["kind"], d["source"], d["title"], d["text"], d["id"], d["created_at"])
        self.conn.execute(f"PRAGMA user_version={VERSION}")
        self.conn.commit()

    # ------------------------------------------------------------- write
    def next_id(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM docs").fetchone()[0]

    def _insert(self, kind, source, title, text, doc_id, created_at) -> int:
        h = self.blobs.put_text(text)
        cur = self.conn.execute(
            "INSERT INTO docs(id, kind, source, title, blob, chars, created_at) VALUES (?,?,?,?,?,?,?)",
            (doc_id, kind, source, title, h, len(text), created_at))
        did = cur.lastrowid
        for k, c in enumerate(chunk(text)):
            self.conn.execute("INSERT INTO chunks(doc_id, idx, start, end, heading) VALUES (?,?,?,?,?)",
                              (did, k, c.start, c.end, c.heading))
            self.conn.execute("INSERT INTO chunks_fts(doc_id, idx, title, heading, body) VALUES (?,?,?,?,?)",
                              (did, k, title or "", c.heading, text[c.start:c.end]))
        return did

    def add(self, kind: str, source: str | None, title: str, text: str, doc_id: int | None = None) -> int:
        """Store a document. An import identical to a live document returns that document's id.
        Callers that reserve ids (promotion) hold the lobby lock, as `Hotel.rag_import` does."""
        if not text.strip():
            raise ValueError("empty document")
        with self._lock:
            if doc_id is None:
                same = self.conn.execute("SELECT id FROM docs WHERE blob=? AND kind=?",
                                         (BlobStore.hash_text(text), kind)).fetchone()
                if same:
                    return same[0]
            did = self._insert(kind, source, title, text, doc_id, utcnow())
            self.conn.commit()
            self._chunks = None
        return did

    def forget(self, doc_id: int) -> dict:
        """Delete a document's content: blob, chunk index rows and vectors. The docs row stays
        (forgotten_at set, title cleared) so RAG:<n> references still resolve to "forgotten"."""
        with self._lock:
            d = self.get(doc_id)
            if d is None:
                raise ValueError(f"no RAG:{doc_id}")
            texts = [embed_text(c) for c in self.chunks(doc_id)]
            self.conn.execute("DELETE FROM chunks_fts WHERE doc_id=?", (doc_id,))
            self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            self.conn.execute("UPDATE docs SET blob=NULL, title=NULL, source=NULL, forgotten_at=? WHERE id=?",
                              (utcnow(), doc_id))
            self.conn.commit()
            self._chunks = None
        deleted = bool(d["blob"]) and self.blobs.delete(d["blob"])
        if self.index is not None:
            self.index.drop(texts)
        return {"blob": d["blob"], "deleted": deleted, "chunks": len(texts)}

    # ------------------------------------------------------------- read
    def get(self, doc_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
        return dict(r) if r else None

    def text(self, doc_id: int) -> str | None:
        d = self.get(doc_id)
        if not d or not d["blob"]:
            return None
        return self.blobs.text(d["blob"])

    def docs(self) -> list[dict]:
        q = """SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS n_chunks
               FROM docs d ORDER BY d.id"""
        return [dict(r) for r in self.conn.execute(q)]

    def chunks(self, doc_id: int | None = None) -> list[dict]:
        q = """SELECT f.doc_id, f.idx, f.title, f.heading, f.body, d.kind, d.source
               FROM chunks_fts f JOIN docs d ON d.id = f.doc_id"""
        args = ()
        if doc_id is not None:
            q, args = q + " WHERE f.doc_id=?", (doc_id,)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY f.doc_id, f.idx", args)]

    def exists(self, ref: str) -> bool:
        """RAG:<n> or RAG:<n>#<k> names a document (or chunk) that was stored, even if since forgotten."""
        m = REF.match(ref)
        if not m:
            return False
        if m.group(2) is None:
            return self.get(int(m.group(1))) is not None
        d = self.get(int(m.group(1)))
        if d and d["forgotten_at"]:
            return True
        return self.conn.execute("SELECT 1 FROM chunks WHERE doc_id=? AND idx=?",
                                 (int(m.group(1)), int(m.group(2)))).fetchone() is not None

    # ------------------------------------------------------------- search
    def _corpus(self) -> list[dict]:
        with self._lock:
            if self._chunks is None:
                self._chunks = self.chunks()
            return self._chunks

    def search(self, q: str, limit: int = 5) -> list[dict]:
        """Hybrid: bm25 over chunk text + heading + title, cosine over embeddings, fused by RRF.
        Each result carries its `signals` (ranks per ranking): future training and eval data."""
        try:
            lex = [f"{r[0]}#{r[1]}" for r in self.conn.execute(
                """SELECT doc_id, idx FROM chunks_fts WHERE chunks_fts MATCH ?
                   ORDER BY bm25(chunks_fts, 0, 0, 2.0, 1.5, 1.0) LIMIT ?""", (fts_query(q), CANDIDATES))]
        except sqlite3.OperationalError:
            lex = []
        rankings = {"bm25": lex}
        corpus = self._corpus()
        if self.index is not None and corpus:
            dense = self.index.rank(q, [embed_text(c) for c in corpus], CANDIDATES)
            if dense is not None:
                rankings["dense"] = [f"{corpus[i]['doc_id']}#{corpus[i]['idx']}" for i, _ in dense]
        by_key = {f"{c['doc_id']}#{c['idx']}": c for c in corpus}
        out = []
        for key, score, ranks in rrf(rankings)[:limit]:
            c = by_key.get(key)
            if c is None:  # deleted between the two reads
                continue
            out.append({**c, "ref": f"RAG:{key}", "signals": {**ranks, "rrf": round(score, 5)}})
        return out


# ------------------------------------------------------------------ importers
class _HTMLText(html.parser.HTMLParser):
    """Visible text of an HTML page; h1-h6 become Markdown headings so chunking follows them."""
    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "li", "br", "tr", "section", "article", "pre", "blockquote", "table", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in self.BLOCK:
            self.out.append("\n\n" if tag != "br" else "\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK:
            self.out.append("\n\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip:
            self.out.append(re.sub(r"[ \t\r\n]+", " ", data))


def read_document(path: Path) -> tuple[str, str]:
    """(title, canonical text) for a file. The canonical text is what is stored, chunked and quoted."""
    suf = path.suffix.lower()
    if suf == ".pdf":
        from pypdf import PdfReader
        pdf = PdfReader(str(path))
        pages = [f"## page {n}\n\n{(p.extract_text() or '').strip()}" for n, p in enumerate(pdf.pages, 1)]
        title = (pdf.metadata.title if pdf.metadata and pdf.metadata.title else "") or path.stem
        return title, "\n\n".join(pages) + "\n"
    raw = path.read_text(encoding="utf-8", errors="replace")
    if suf in (".html", ".htm"):
        p = _HTMLText()
        p.feed(raw)
        text = re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip() + "\n"
        return (p.title.strip() or path.stem), text
    m = re.search(r"^#[ \t]+(.+)$", raw, flags=re.M)
    return (m.group(1).strip() if m else path.name), raw


IMPORTABLE = {".md", ".markdown", ".txt", ".rst", ".org", ".html", ".htm", ".pdf"}


def importable_files(paths: list[Path]) -> list[Path]:
    out = []
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in IMPORTABLE
                          and not any(part.startswith(".") for part in f.relative_to(p).parts))
        elif p.is_file():
            out.append(p)
        else:
            raise FileNotFoundError(p)
    return out
