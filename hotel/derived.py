"""Derived layers: hybrid index over lobby entries + directory file.
Everything here is rebuildable from lobby.txt alone (vectors are a cache keyed
by text hash, so a rebuild re-embeds nothing)."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from .concierge.index import VectorIndex, rrf
from .grammar import Lobby, parse


def fts_query(q: str) -> str:
    toks = re.findall(r"[\w\-']+", q, flags=re.UNICODE)
    return " OR ".join('"' + t.replace('"', "") + '"' for t in toks) if toks else '""'


class Derived:
    def __init__(self, db_path: Path, index: VectorIndex | None = None):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.index = index
        self.entries: list[dict] = []  # current entries, for dense search

    def rebuild(self, text: str, directory_path: Path | None = None) -> Lobby:
        lob = parse(text)
        c = self.conn
        c.execute("DROP TABLE IF EXISTS lobby_fts")
        c.execute("CREATE VIRTUAL TABLE lobby_fts USING fts5(eid UNINDEXED, room, type, status UNINDEXED, body, tags)")
        c.executemany(
            "INSERT INTO lobby_fts VALUES (?,?,?,?,?,?)",
            [(e.id, e.room, e.type, e.status, e.text, " ".join(e.metas("tag"))) for e in lob.entries.values()],
        )
        c.commit()
        self.entries = [dict(eid=e.id, room=e.room, type=e.type, status=e.status, body=e.text) for e in lob.entries.values()]
        if directory_path is not None:
            directory_path.write_text(render_directory(lob, text), encoding="utf-8", newline="\n")
        return lob

    def search(self, q: str, limit: int = 10, include_dead: bool = False) -> list[dict]:
        """Hybrid: bm25 and (with an embedder) cosine similarity, fused by RRF. Superseded
        entries are dropped unless include_dead. Each row carries its ranks in `signals`."""
        try:
            rows = self.conn.execute(
                """SELECT eid, room, type, status, body,
                          snippet(lobby_fts, 4, '[', ']', ' … ', 16) AS snip, bm25(lobby_fts) AS score
                   FROM lobby_fts WHERE lobby_fts MATCH ? ORDER BY score LIMIT ?""",
                (fts_query(q), max(limit * 3, 30)),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        alive = lambda r: include_dead or r["status"] != "superseded"
        lex = {r["eid"]: dict(r) for r in rows if alive(r)}
        rankings = {"bm25": list(lex)}
        pool = [e for e in self.entries if alive(e)]
        if self.index is not None and pool:
            dense = self.index.rank(q, [f"{e['type']} in {e['room']}: {e['body']}" for e in pool], 50)
            if dense is not None:
                rankings["dense"] = [pool[i]["eid"] for i, _ in dense]
        by_id = {e["eid"]: e for e in pool}
        out = []
        for eid, score, ranks in rrf(rankings)[:limit]:
            row = lex.get(eid) or {**by_id[eid], "snip": " ".join(by_id[eid]["body"].split())[:200], "score": None}
            out.append({**row, "signals": {**ranks, "rrf": round(score, 5)}})
        return out


def render_directory(lob: Lobby, text: str) -> str:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    live = [e for e in lob.entries.values() if e.sigil in (" ", "!")]
    out = [
        "DIRECTORY — generated from lobby.txt, do not edit (hotel rebuild)",
        f"lobby {h}   entries {len(lob.entries)}   open {len(live)}",
        "",
    ]
    for room in lob.rooms:
        ents = lob.in_room(room)
        open_ = [e for e in ents if e.sigil in (" ", "!")]
        out.append(f"== {room} ==  open {len(open_)} / {len(ents)}")
        for e in open_:
            first = e.body[0][:70] if e.body else ""
            out.append(f"  [{e.sigil}] {e.id} {e.type:<9} {first}")
        out.append("")
    return "\n".join(out)
