"""Replay harness: re-run a recorded Reg session against a different librarian
(or prompt) and diff the resulting lobby against what actually happened.

Walk the session in seq order, starting from the lobby snapshot taken at
session start:
  * user/approved lobby_edit rows -> re-apply recorded ops (ids remapped)
  * hand edits                     -> cannot be replayed op-wise; reported
  * librarian lobby_edit / review  -> dropped; replaced by the new librarian
  * annotation rows                -> run the new librarian on it
No network for recorded data: retrievals are read from blobs. A model
librarian still makes fresh model calls, logged to .hotel/replay.sqlite.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from .gateway import Gateway, load_annotation
from .grammar import parse
from .librarian import Librarian, ModelLibrarian, RuleLibrarian, build_context
from .models import AppendOp, CloseOp, ContestOp, PromoteOp, ReopenOp, SupersedeOp, op_load
from .ops import OpError, apply_op, remap_op
from .reg import Reg
from .validator import validate_transition


@dataclass
class ReplayReport:
    librarian: str
    final_text: str
    actual_text: str
    log: list[str] = field(default_factory=list)
    idmap: dict[str, str] = field(default_factory=dict)

    def diff(self) -> str:
        return "".join(difflib.unified_diff(
            self.actual_text.splitlines(True), self.final_text.splitlines(True),
            "lobby (recorded)", f"lobby (replay: {self.librarian})",
        ))


class _ShadowHotel:
    """Just enough of Hotel for build_context: the reg and an FTS over the replay lobby."""

    def __init__(self, hotel, text):
        from pathlib import Path

        from .derived import Derived
        self.reg = hotel.reg
        self.blobs = hotel.blobs
        self.cfg = hotel.cfg
        self.derived = Derived(Path(":memory:"))  # lexical only: replay stays deterministic
        self.set_text(text)

    def set_text(self, text):
        self.text = text
        self.derived.rebuild(text)

    def lobby(self):
        return parse(self.text)


def replay(hotel, session_id: str, librarian: Librarian | str = "rule",
           approve_queued: bool = False) -> ReplayReport:
    sess = hotel.reg.session(session_id)
    if not sess:
        raise ValueError(f"no session {session_id}")
    text = hotel.blobs.text(sess["lobby_snapshot"])
    rows = hotel.reg.session_entries(session_id)
    actual = text
    for r in rows:
        if r.kind == "lobby_edit" and r.status == "ok":
            actual = hotel.blobs.text(r.payload["after"])

    shadow = _ShadowHotel(hotel, text)
    if isinstance(librarian, str):
        if librarian != "rule":
            rreg = Reg(hotel.paths.replay_db)
            sid = rreg.start_session(f"replay of {session_id}")
            gw = Gateway(rreg, hotel.blobs, sid, lambda: set(shadow.lobby().entries))
            librarian = ModelLibrarian(hotel, None if librarian == "model" else librarian, gateway=gw)
        else:
            librarian = RuleLibrarian()
    rep = ReplayReport(librarian.name, text, actual)
    rag_counter = hotel.rag.next_id() + 1000  # synthetic ids, never written

    def apply_list(ops, origin, ts, label, record_ids=None, days="cfg"):
        nonlocal text, rag_counter
        days = hotel.cfg.get("bounty_revalidate_days") if days == "cfg" else days
        before = text
        try:
            for i, op in enumerate(ops):
                op = remap_op(op, rep.idmap)
                rid = None
                if isinstance(op, PromoteOp):
                    rid, rag_counter = rag_counter, rag_counter + 1
                res = apply_op(text, op, origin=origin, ts=ts, rag_id=rid,
                               revalidate_days=days)
                text = res.text
                if record_ids and i < len(record_ids):
                    for old_id, new_id in zip(record_ids[i], res.created):
                        rep.idmap[old_id] = new_id
            errs = validate_transition(before, text, hotel.oracle)
            if errs:
                raise OpError("; ".join(errs[:2]))
            rep.log.append(f"{label}: ok")
        except Exception as ex:  # noqa: BLE001 — every failure is reported, not hidden
            text = before
            rep.log.append(f"{label}: DROPPED ({ex})")
        shadow.set_text(text)

    for r in rows:
        if r.kind == "lobby_edit" and r.status == "ok":
            p = r.payload
            if p.get("hand_edit"):
                rep.log.append(f"R:{r.id} hand edit: not replayable op-wise, skipped")
            elif r.actor == "librarian":
                rep.log.append(f"R:{r.id} recorded librarian edit: replaced")
            elif p.get("origin") == "approved":
                rep.log.append(f"R:{r.id} recorded approval of librarian op: replaced")
            else:
                ops = [op_load(o) for o in p["ops"]]
                apply_list(ops, p.get("origin", "user"), p.get("ts") or r.ts,
                           f"R:{r.id} user edit", p.get("created"), days=p.get("revalidate_days"))
        elif r.kind == "annotation" and r.status == "ok":
            ctx = build_context(shadow, r.ref_id, shadow.lobby())
            out = librarian.propose(ctx)
            rep.log.append(f"R:{r.id} librarian on R:{r.ref_id}: {len(out.ops)} ops — {out.rationale[:80]}")
            for op in out.ops:
                if isinstance(op, AppendOp) and op.type != "log":
                    rep.log.append(f"   {op.op}: rejected (resident non-log append)")
                    continue
                queued = isinstance(op, (SupersedeOp, ContestOp, ReopenOp, PromoteOp))
                if isinstance(op, CloseOp):  # same deterministic rule as live policy
                    ann_row = hotel.reg.annotation_for(int(op.closed_by[2:])) if op.closed_by.startswith("R:") else None
                    ann = load_annotation(ann_row, hotel.blobs) if ann_row and ann_row.actor == "user" else None
                    queued = not (ann and any(c["relation"] == "supports" and op.id in c["lobby_refs"]
                                              for c in ann["claims"]))
                if queued and not approve_queued:
                    rep.log.append(f"   {op.op} {getattr(op, 'id', '')}: queued (not applied)")
                    continue
                apply_list([op], "approved" if queued else "resident", r.ts, f"   {op.op} {getattr(op, 'id', '')}")
    rep.final_text = text
    return rep
