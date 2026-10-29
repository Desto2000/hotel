"""Lobby validator: static checks on one text, and transition checks old->new.

The transition check is what makes the lobby append-only: every old entry
must survive with identical room/type/origin/ts/body, its meta lines may only
be extended, and its sigil may only move along TRANSITIONS.
"""

from __future__ import annotations

from typing import Protocol

from .grammar import (
    STATIC, TRANSITIONS, Lobby, LobbyParseError, creation_sigil, parse,
)


class RegOracle(Protocol):
    def reg_exists(self, rid: int) -> bool: ...
    def is_annotated_retrieval(self, rid: int) -> bool: ...


def _rid(ref: str) -> int | None:
    if ref.startswith("R:"):
        return int(ref[2:].split("#")[0])
    return None


def check_static(lob: Lobby, oracle: RegOracle | None = None) -> list[str]:
    errs: list[str] = []
    E = lob.entries
    for e in E.values():
        for k, v in e.meta:
            target = v.split("#")[0]
            if k in ("link", "supersedes", "superseded-by") or (k in ("ref", "closed-by") and target.startswith("L")):
                if target not in E:
                    errs.append(f"{e.id}: {k} -> {target} does not exist")
                elif target == e.id:
                    errs.append(f"{e.id}: {k} points at itself")
            rid = _rid(v) if k in ("ref", "closed-by") else None
            if rid is not None and oracle and not oracle.reg_exists(rid):
                errs.append(f"{e.id}: {k} -> {v} not in Reg")

        if e.origin == "resident" and e.type != "log":
            errs.append(f"{e.id}: residents may only author 'log' entries, not {e.type}")

        sb = e.metas("superseded-by")
        if e.sigil == "~":
            if not sb:
                errs.append(f"{e.id}: superseded without superseded-by")
            for n in sb:
                if n in E and e.id not in E[n].metas("supersedes"):
                    errs.append(f"{e.id}: superseded-by {n} but {n} lacks 'supersedes: {e.id}'")
        elif sb:
            errs.append(f"{e.id}: has superseded-by but is not marked [~]")
        for old in e.metas("supersedes"):
            if old in E and (E[old].sigil != "~" or e.id not in E[old].metas("superseded-by")):
                errs.append(f"{e.id}: supersedes {old} but {old} is not marked superseded by {e.id}")

        if e.sigil == "x":
            cb = e.metas("closed-by")
            if e.type == "bounty":
                rids = [_rid(c) for c in cb if c.startswith("R:")]
                if not rids:
                    errs.append(f"{e.id}: closed bounty needs 'closed-by: R:<n>' (an annotated retrieval)")
                elif oracle and not any(oracle.is_annotated_retrieval(r) for r in rids):
                    errs.append(f"{e.id}: closed-by {cb} is not an annotated retrieval")
    return errs


def validate_text(text: str, oracle: RegOracle | None = None) -> tuple[Lobby | None, list[str]]:
    try:
        lob = parse(text)
    except LobbyParseError as ex:
        return None, [f"parse: line {n}: {m}" for n, m in ex.errors]
    return lob, check_static(lob, oracle)


def validate_transition(old_text: str, new_text: str, oracle: RegOracle | None = None) -> list[str]:
    try:
        old = parse(old_text)  # old was accepted when written: parse it, don't re-check it
    except LobbyParseError as ex:
        return ["previous lobby does not parse: " + "; ".join(f"line {n}: {m}" for n, m in ex.errors[:3])]
    new, errs = validate_text(new_text, oracle)
    if new is None:
        return errs

    if new.rooms[: len(old.rooms)] != old.rooms:
        errs.append("rooms may only be appended (existing room order/name changed or removed)")

    for oid, o in old.entries.items():
        n = new.entries.get(oid)
        if n is None:
            errs.append(f"{oid}: deleted (entries are never deleted; supersede instead)")
            continue
        for f in ("room", "type", "origin", "ts"):
            if getattr(o, f) != getattr(n, f):
                errs.append(f"{oid}: {f} changed {getattr(o, f)!r} -> {getattr(n, f)!r}")
        if o.body != n.body:
            errs.append(f"{oid}: body changed (write a superseding entry instead)")
        if n.meta[: len(o.meta)] != o.meta:
            errs.append(f"{oid}: existing meta lines altered/removed (meta is append-only)")
        added = n.meta[len(o.meta):]
        if any(k == "supersedes" for k, _ in added):
            errs.append(f"{oid}: 'supersedes' may only be set when an entry is created")
        if o.sigil != n.sigil and n.sigil not in TRANSITIONS[o.sigil]:
            errs.append(f"{oid}: illegal status [{o.sigil}] -> [{n.sigil}]")

    max_old = old.max_num()
    for nid, n in new.entries.items():
        if nid in old.entries:
            continue
        if n.num <= max_old:
            errs.append(f"{nid}: new ids must be greater than L{max_old:04d}")
        if n.sigil != creation_sigil(n.type):
            errs.append(f"{nid}: new {n.type} must start as [{creation_sigil(n.type)}]")

    for room in old.rooms:
        o_ids = [e.id for e in old.in_room(room)]
        n_ids = [e.id for e in new.in_room(room)]
        if n_ids[: len(o_ids)] != o_ids:
            errs.append(f"room {room}: entries may only be appended at the end of the room")
    return errs


__all__ = ["RegOracle", "check_static", "validate_text", "validate_transition", "STATIC"]
