"""Derived layers: FTS5 lexical index over lobby entries + directory file.
Everything here is rebuildable from lobby.txt alone."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from .grammar import Lobby, parse


def fts_query(q: str) -> str:
    toks = re.findall(r"[\w\-']+", q, flags=re.UNICODE)
    return " OR ".join('"' + t.replace('"', "") + '"' for t in toks) if toks else '""'


class Derived:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

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
        if directory_path is not None:
            directory_path.write_text(render_directory(lob, text), encoding="utf-8", newline="\n")
        return lob

    def search(self, q: str, limit: int = 10, include_dead: bool = False) -> list[dict]:
        try:
            rows = self.conn.execute(
                """SELECT eid, room, type, status, body,
                          snippet(lobby_fts, 4, '[', ']', ' … ', 16) AS snip, bm25(lobby_fts) AS score
                   FROM lobby_fts WHERE lobby_fts MATCH ? ORDER BY score LIMIT ?""",
                (fts_query(q), limit * 2),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out = [dict(r) for r in rows]
        if not include_dead:
            out = [r for r in out if r["status"] != "superseded"]
        return out[:limit]


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
