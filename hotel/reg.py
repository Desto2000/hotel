"""The Reg: one immutable, hash-chained, append-only log.

Immutability is enforced by SQLite triggers (UPDATE/DELETE abort), not by
convention. Every row carries prev_hash/entry_hash so tampering via a raw
file edit is detectable with `verify_chain()`.

Row kinds:
  retrieval   external knowledge fetched (Exa, RAG, unified search) — gated
  internal    own-data lookup (lobby search) — logged, not gated
  model       model call (librarian) — logged, gated
  annotation  structured annotation of a retrieval (ref_id = retrieval)
  lobby_edit  an applied lobby change (payload: ops, before/after blobs, commit)
  review      an op parked in the review queue
  decision    approve/reject of a review item (ref_id = review row)
  output      final output text — gated
  blocked     a call refused by the annotation gate
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    label TEXT,
    lobby_snapshot TEXT
);
CREATE TABLE IF NOT EXISTS reg (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    tool TEXT,
    provider TEXT,
    payload TEXT,
    ref_id INTEGER,
    result_hash TEXT,
    text_hash TEXT,
    status TEXT NOT NULL,
    error TEXT,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reg_kind ON reg(kind);
CREATE INDEX IF NOT EXISTS reg_ref ON reg(ref_id);
CREATE INDEX IF NOT EXISTS reg_session ON reg(session_id, seq);
CREATE TRIGGER IF NOT EXISTS reg_no_update BEFORE UPDATE ON reg
BEGIN SELECT RAISE(ABORT, 'reg is append-only'); END;
CREATE TRIGGER IF NOT EXISTS reg_no_delete BEFORE DELETE ON reg
BEGIN SELECT RAISE(ABORT, 'reg is append-only'); END;
CREATE TRIGGER IF NOT EXISTS sessions_no_update BEFORE UPDATE ON sessions
BEGIN SELECT RAISE(ABORT, 'sessions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS sessions_no_delete BEFORE DELETE ON sessions
BEGIN SELECT RAISE(ABORT, 'sessions are append-only'); END;
"""

GENESIS = "0" * 64
HASHED_FIELDS = (
    "session_id", "seq", "ts", "kind", "actor", "tool", "provider", "payload",
    "ref_id", "result_hash", "text_hash", "status", "error", "prev_hash",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RegEntry:
    id: int
    session_id: str
    seq: int
    ts: str
    kind: str
    actor: str
    tool: str | None
    provider: str | None
    payload: Any
    ref_id: int | None
    result_hash: str | None
    text_hash: str | None
    status: str
    error: str | None
    prev_hash: str
    entry_hash: str

    @property
    def rid(self) -> str:
        return f"R:{self.id}"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RegEntry":
        d = dict(row)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else None
        return cls(**d)


def _hash_row(fields: dict) -> str:
    canon = json.dumps({k: fields[k] for k in HASHED_FIELDS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class Reg:
    def __init__(self, db_path: Path, events=None):
        self.db_path = db_path
        self.events = events  # EventLog mirror for the live timeline (None: replay / tests)
        self.conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        # One connection shared by TUI worker threads: a transaction belongs to the
        # connection, so appends from two threads must not interleave.
        self._lock = threading.Lock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------- sessions
    def start_session(self, label: str | None = None, lobby_snapshot: str | None = None) -> str:
        sid = utcnow().replace(":", "").replace("-", "") + "-" + uuid.uuid4().hex[:6]
        self.conn.execute(
            "INSERT INTO sessions(id, started_at, label, lobby_snapshot) VALUES (?,?,?,?)",
            (sid, utcnow(), label, lobby_snapshot),
        )
        if self.events:
            self.events.emit("session", id=sid, label=label)
        return sid

    def session(self, sid: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None

    def sessions(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM sessions ORDER BY started_at")]

    # ------------------------------------------------------------- append
    def append(
        self,
        session_id: str,
        kind: str,
        actor: str,
        *,
        tool: str | None = None,
        provider: str | None = None,
        payload: Any = None,
        ref_id: int | None = None,
        result_hash: str | None = None,
        text_hash: str | None = None,
        status: str = "ok",
        error: str | None = None,
        ts: str | None = None,
    ) -> RegEntry:
        c = self.conn
        with self._lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                seq = c.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM reg WHERE session_id=?", (session_id,)
                ).fetchone()[0]
                last = c.execute("SELECT entry_hash FROM reg ORDER BY id DESC LIMIT 1").fetchone()
                fields = dict(
                    session_id=session_id, seq=seq, ts=ts or utcnow(), kind=kind, actor=actor,
                    tool=tool, provider=provider,
                    payload=json.dumps(payload, sort_keys=True, ensure_ascii=False) if payload is not None else None,
                    ref_id=ref_id, result_hash=result_hash, text_hash=text_hash, status=status,
                    error=error, prev_hash=last[0] if last else GENESIS,
                )
                fields["entry_hash"] = _hash_row(fields)
                cols = ",".join(fields)
                cur = c.execute(
                    f"INSERT INTO reg({cols}) VALUES ({','.join('?' * len(fields))})", tuple(fields.values())
                )
                rid = cur.lastrowid
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        entry = self.get(rid)
        if self.events:
            self.events.reg_row(entry)
        return entry

    # ------------------------------------------------------------- read
    def get(self, rid: int) -> RegEntry | None:
        r = self.conn.execute("SELECT * FROM reg WHERE id=?", (rid,)).fetchone()
        return RegEntry.from_row(r) if r else None

    def list(self, session_id: str | None = None, kind: str | None = None, limit: int = 500,
             tool: str | None = None, ref_id: int | None = None) -> list[RegEntry]:
        """The latest `limit` rows matching every given filter, oldest first."""
        q, args = "SELECT * FROM reg WHERE 1=1", []
        for col, val in (("session_id", session_id), ("kind", kind), ("tool", tool), ("ref_id", ref_id)):
            if val is not None:
                q += f" AND {col}=?"
                args.append(val)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [RegEntry.from_row(r) for r in self.conn.execute(q, args)][::-1]

    def session_entries(self, session_id: str) -> list[RegEntry]:
        return [
            RegEntry.from_row(r)
            for r in self.conn.execute("SELECT * FROM reg WHERE session_id=? ORDER BY seq", (session_id,))
        ]

    def annotation_for(self, rid: int) -> RegEntry | None:
        r = self.conn.execute(
            "SELECT * FROM reg WHERE kind='annotation' AND ref_id=? AND status='ok' ORDER BY id LIMIT 1", (rid,)
        ).fetchone()
        return RegEntry.from_row(r) if r else None

    def latest(self, kind: str, tool: str, ref_id: int) -> RegEntry | None:
        r = self.conn.execute(
            "SELECT * FROM reg WHERE ref_id=? AND kind=? AND tool=? AND status='ok' ORDER BY id DESC LIMIT 1",
            (ref_id, kind, tool),
        ).fetchone()
        return RegEntry.from_row(r) if r else None

    def unannotated_retrievals(self) -> list[RegEntry]:
        """Global, not per-session: starting a new session must not escape the gate.
        A forgotten retrieval cannot be annotated any more, so it no longer holds the gate."""
        rows = self.conn.execute(
            """SELECT * FROM reg r WHERE r.kind='retrieval' AND r.status='ok'
               AND NOT EXISTS (SELECT 1 FROM reg a WHERE a.kind='annotation' AND a.status='ok' AND a.ref_id=r.id)
               AND NOT EXISTS (SELECT 1 FROM reg x WHERE x.kind='redaction' AND x.ref_id=r.id)
               ORDER BY r.id"""
        )
        return [RegEntry.from_row(r) for r in rows]

    def count_recent(self, kind: str, actor: str, since_ts: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM reg WHERE kind=? AND actor=? AND ts>=? AND status='ok'", (kind, actor, since_ts)
        ).fetchone()[0]

    # ------------------------------------------------------------- review queue
    def pending_reviews(self) -> list[RegEntry]:
        rows = self.conn.execute(
            """SELECT * FROM reg r WHERE r.kind='review'
               AND NOT EXISTS (SELECT 1 FROM reg d WHERE d.kind='decision' AND d.ref_id=r.id)
               ORDER BY r.id"""
        )
        return [RegEntry.from_row(r) for r in rows]

    # ------------------------------------------------------------- integrity
    def verify_chain(self) -> list[str]:
        problems, prev = [], GENESIS
        for r in self.conn.execute("SELECT * FROM reg ORDER BY id"):
            d = dict(r)
            if d["prev_hash"] != prev:
                problems.append(f"R:{d['id']} prev_hash mismatch")
            if _hash_row(d) != d["entry_hash"]:
                problems.append(f"R:{d['id']} entry_hash mismatch (row altered)")
            prev = d["entry_hash"]
        return problems
