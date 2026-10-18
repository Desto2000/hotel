"""Pure text-level application of edit ops (no IO). Used by the store and by
the replay harness, so both produce byte-identical lobbies."""

from __future__ import annotations

from dataclasses import dataclass

from .grammar import ROOM_SLUG, TRANSITIONS, creation_sigil, parse, render_entry
from .models import (
    AppendOp, CloseOp, ContestOp, LinkOp, MoveOp, PromoteOp, RefOp, ReopenOp, SupersedeOp, TagOp,
)


class OpError(Exception):
    pass


@dataclass
class OpResult:
    text: str
    created: list[str]
    touched: list[str]
    noop: bool = False


def _join(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _need(lob, eid):
    e = lob.entries.get(eid)
    if e is None:
        raise OpError(f"no such entry {eid}")
    return e


def _set_sigil(lines, e, sigil):
    if sigil not in TRANSITIONS[e.sigil]:
        raise OpError(f"{e.id}: cannot go [{e.sigil}] -> [{sigil}]")
    head = lines[e.start]
    pos = len(f"@{e.id} [")
    lines[e.start] = head[:pos] + sigil + head[pos + 1:]


def _add_meta(lines, e, key, val):
    lines.insert(e.end + 1, f"  + {key}: {val}")


def _append_entry(lob, lines, room, block):
    if not ROOM_SLUG.match(room):
        raise OpError(f"bad room name {room!r} (lowercase slug)")
    if room in lob.room_line:
        ents = lob.in_room(room)
        pos = (ents[-1].end + 1) if ents else (lob.room_line[room] + 1)
        lines[pos:pos] = [""] + block
    else:
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", f"== ROOM: {room} ==", ""] + block


def apply_op(text: str, op, *, origin: str, ts: str, rag_id: int | None = None,
             revalidate_days: int | None = None) -> OpResult:
    lob = parse(text)
    lines = list(lob.lines)

    if isinstance(op, MoveOp):
        e = _need(lob, op.id)
        if e.sigil not in (" ", "."):
            raise OpError(f"only open/static entries move (closed/contested history stays put); {e.id} is [{e.sigil}]")
        if op.room == e.room:
            raise OpError(f"{e.id} is already in {op.room}")
        nid = lob.next_id()
        _add_meta(lines, e, "superseded-by", nid)
        lines.insert(e.end + 2, f"  + note: moved to {op.room} — {' '.join(op.reason.split()) or 'refiled'}")
        _set_sigil(lines, e, "~")
        lob2 = parse(_join(lines))
        keep = [(k, v) for k, v in e.meta if k in ("tag", "ref", "link")]
        block = render_entry(nid, e.sigil, e.type, e.origin, ts, e.text,
                             [("supersedes", e.id), ("moved-from", e.room)] + keep)
        lines2 = list(lob2.lines)
        _append_entry(lob2, lines2, op.room, block)
        return OpResult(_join(lines2), [nid], [e.id])

    if isinstance(op, AppendOp):
        nid = lob.next_id()
        meta = [("ref", r) for r in op.refs] + [("tag", t) for t in op.tags]
        block = render_entry(nid, creation_sigil(op.type), op.type, origin, ts, op.body, meta)
        _append_entry(lob, lines, op.room, block)
        return OpResult(_join(lines), [nid], [])

    if isinstance(op, (TagOp, LinkOp, RefOp)):
        e = _need(lob, op.id)
        key, val = {TagOp: ("tag", getattr(op, "tag", None)),
                    LinkOp: ("link", getattr(op, "target", None)),
                    RefOp: ("ref", getattr(op, "ref", None))}[type(op)]
        if (key, val) in e.meta:
            return OpResult(text, [], [], noop=True)
        _add_meta(lines, e, key, val)
        return OpResult(_join(lines), [], [e.id])

    if isinstance(op, CloseOp):
        e = _need(lob, op.id)
        if e.type == "bounty" and not op.closed_by.startswith("R:"):
            raise OpError("a bounty closes only through a Reg retrieval (closed-by: R:<n>)")
        _add_meta(lines, e, "closed-by", op.closed_by)
        if e.type == "bounty" and revalidate_days:
            # web facts expire: the custodian flags this bounty when the date passes
            from datetime import datetime, timedelta
            due = (datetime.strptime(ts[:10], "%Y-%m-%d") + timedelta(days=revalidate_days)).strftime("%Y-%m-%d")
            lines.insert(e.end + 2, f"  + revalidate: {due}")
        _set_sigil(lines, e, "x")
        return OpResult(_join(lines), [], [e.id])

    if isinstance(op, SupersedeOp):
        e = _need(lob, op.id)
        if e.sigil == "~":
            raise OpError(f"{e.id} is already superseded")
        nid = lob.next_id()
        _add_meta(lines, e, "superseded-by", nid)
        if op.reason.strip():
            lines.insert(e.end + 2, f"  + note: superseded — {' '.join(op.reason.split())}")
        _set_sigil(lines, e, "~")
        lob2 = parse(_join(lines))
        block = render_entry(nid, creation_sigil(op.type), op.type, origin, ts, op.body, [("supersedes", e.id)])
        lines2 = list(lob2.lines)
        _append_entry(lob2, lines2, e.room, block)
        return OpResult(_join(lines2), [nid], [e.id])

    if isinstance(op, ContestOp):
        e = _need(lob, op.id)
        _add_meta(lines, e, "note", f"contested — {' '.join(op.reason.split())}")
        _set_sigil(lines, e, "!")
        return OpResult(_join(lines), [], [e.id])

    if isinstance(op, ReopenOp):
        e = _need(lob, op.id)
        if e.sigil != "!":
            raise OpError("only contested entries can be reopened")
        _add_meta(lines, e, "note", f"reopened — {' '.join(op.reason.split())}")
        _set_sigil(lines, e, " ")
        return OpResult(_join(lines), [], [e.id])

    if isinstance(op, PromoteOp):
        e = _need(lob, op.id)
        if e.sigil == "~":
            raise OpError("cannot promote a superseded entry")
        if rag_id is None:
            raise OpError("promote needs a RAG id")
        _add_meta(lines, e, "promoted", f"RAG:{rag_id}")
        return OpResult(_join(lines), [], [e.id])

    raise OpError(f"unknown op {op!r}")


def remap_op(op, idmap: dict[str, str]):
    """Rewrite lobby ids inside an op (replay harness)."""
    def m(v: str) -> str:
        head, sep, frag = v.partition("#")
        return idmap.get(head, head) + sep + frag

    d = op.model_dump()
    for k in ("id", "target", "closed_by", "ref"):
        if k in d and isinstance(d[k], str):
            d[k] = m(d[k])
    if "refs" in d:
        d["refs"] = [m(r) for r in d["refs"]]
    return type(op).model_validate(d)
