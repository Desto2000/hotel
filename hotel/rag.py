"""RAG store: imported + promoted knowledge. References are one-way: the lobby
cites RAG:<n>; RAG documents never cite the lobby. Promotion provenance lives
in the Reg (the lobby_edit row), not in the RAG document."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .derived import fts_query
from .reg import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,         -- import | promotion
    source TEXT,
    title TEXT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(title, text);
"""


class Rag:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def next_id(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM docs").fetchone()[0]

    def add(self, kind: str, source: str, title: str, text: str, doc_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO docs(id, kind, source, title, text, created_at) VALUES (?,?,?,?,?,?)",
            (doc_id, kind, source, title, text, utcnow()),
        )
        rid = cur.lastrowid
        self.conn.execute("INSERT INTO docs_fts(rowid, title, text) VALUES (?,?,?)", (rid, title, text))
        self.conn.commit()
        return rid

    def get(self, doc_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
        return dict(r) if r else None

    def search(self, q: str, limit: int = 5) -> list[dict]:
        try:
            rows = self.conn.execute(
                """SELECT d.*, snippet(docs_fts, 1, '[', ']', ' … ', 20) AS snip
                   FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid
                   WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts) LIMIT ?""",
                (fts_query(q), limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]
