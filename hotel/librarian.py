"""Librarian: proposes structured edit ops from an annotated retrieval.

Swappable behind `Librarian.propose(ctx) -> LibrarianOutput`:
  RuleLibrarian     deterministic baseline, no network (also the replay control)
  MercuryLibrarian  Inception Mercury 2.5 via OpenAI-compatible API with
                    json_schema structured output; call is logged through the
                    gateway as a `model` row (prompt + response stored by hash).

Ops never apply directly: they go through Hotel.route_resident_op (policy:
rate cap, log-only appends, deterministic close check, review queue).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

import httpx

from .gateway import ToolResult, ToolSpec
from .grammar import Lobby
from .models import (
    AppendOp, Annotation, CloseOp, ContestOp, LibrarianOutput, RefOp,
)

GRAMMAR_BRIEF = """Lobby rules: entries have ids like L0007, a room, a type
(monologue|bounty|goal|reasoning|idea|log) and a status ([ ] open, [x] closed,
[~] superseded, [!] contested, [.] static). You are a RESIDENT librarian:
- you may append entries of type "log" only (never goals, beliefs, ideas)
- you may tag, link (id->id) and ref (id -> R:<n> | L#### | RAG:<n>)
- close a bounty only with closed_by "R:<n>" of an annotated retrieval that supports it
- supersede/contest/reopen/promote are allowed but go to human review
- body text: plain, short, no markdown. Prefer few ops; zero ops is fine."""


@dataclass
class LibrarianContext:
    reg_id: int
    annotation: Annotation
    claims_text: str
    entries_text: str
    rooms: list[str]
    lobby: Lobby


class Librarian(Protocol):
    name: str

    def propose(self, ctx: LibrarianContext) -> LibrarianOutput: ...


def build_context(hotel, reg_id: int, lobby: Lobby | None = None) -> LibrarianContext:
    ann_row = hotel.reg.annotation_for(reg_id)
    if ann_row is None:
        raise ValueError(f"R:{reg_id} has no annotation")
    ann = Annotation.model_validate(ann_row.payload)
    lob = lobby or hotel.lobby()
    ids: list[str] = []
    for c in ann.claims:
        ids += [r for r in c.lobby_refs if r in lob.entries]
    ids += [r for r in ann.caused_refs if r in lob.entries]
    for c in ann.claims:
        ids += [h["eid"] for h in hotel.derived.search(c.text, 3)]
    seen, picked = set(), []
    for i in ids:
        if i not in seen and i in lob.entries:
            seen.add(i)
            picked.append(lob.entries[i])
    entries_text = "\n\n".join(
        "\n".join(lob.lines[e.start:e.end + 1]) for e in picked[:12]
    ) or "(no related entries)"
    claims_text = "\n".join(
        f"- [{c.relation}] {c.text}  refs={c.lobby_refs}  quote=\"{c.pointer.quote[:200]}\"" for c in ann.claims
    ) or "(no claims)"
    claims_text += f"\ncaused: {ann.caused}  caused_refs={ann.caused_refs}"
    return LibrarianContext(reg_id, ann, claims_text, entries_text, lob.rooms, lob)


class RuleLibrarian:
    name = "rule-v1"

    def propose(self, ctx: LibrarianContext) -> LibrarianOutput:
        ops, seen = [], set()
        rid = f"R:{ctx.reg_id}"
        for c in ctx.annotation.claims:
            for ref in c.lobby_refs:
                e = ctx.lobby.entries.get(ref)
                if not e:
                    continue
                if ref not in seen:
                    seen.add(ref)
                    ops.append(RefOp(op="ref", id=ref, ref=rid))
                if c.relation == "supports" and e.type == "bounty" and e.sigil in (" ", "!") \
                        and ref in ctx.annotation.caused_refs:
                    ops.append(CloseOp(op="close", id=ref, closed_by=rid))
                if c.relation == "contradicts":
                    if e.type in ("bounty", "goal", "idea") and e.sigil == " ":
                        ops.append(ContestOp(op="contest", id=ref, reason=f"{rid}: {c.text}"))
                    else:
                        ops.append(AppendOp(op="append", room=e.room, type="log",
                                            body=f"{rid} contradicts {ref}: {c.text}",
                                            refs=[rid, ref], tags=["contradiction"]))
        # dedupe identical ops
        uniq, keys = [], set()
        for o in ops:
            k = json.dumps(o.model_dump(), sort_keys=True)
            if k not in keys:
                keys.add(k)
                uniq.append(o)
        return LibrarianOutput(rationale="deterministic rules over annotation relations", ops=uniq)


class MercuryLibrarian:
    def __init__(self, hotel, model: str | None = None, base_url: str | None = None, gateway=None):
        self.hotel = hotel
        self.model = model or hotel.cfg["mercury_model"]
        self.base_url = base_url or hotel.cfg["mercury_base_url"]
        self.name = f"mercury:{self.model}"
        self.gateway = gateway or hotel.gateway
        if "mercury.chat" not in self.gateway.tools:
            self.gateway.register(ToolSpec("mercury.chat", "model", "inception", self._chat))

    def _chat(self, messages: list[dict], schema: dict) -> ToolResult:
        key = os.environ.get("INCEPTION_API_KEY")
        if not key:
            raise RuntimeError("INCEPTION_API_KEY not set")
        if not (self.hotel.cfg.get("inception_training_opt_out_confirmed")
                or os.environ.get("HOTEL_INCEPTION_TRAINING_OPTOUT") == "confirmed"):
            raise RuntimeError(
                "refusing to send lobby content: disable the API training opt-in in the Inception "
                "dashboard, then set inception_training_opt_out_confirmed=true in .hotel/config.json"
            )
        body = {
            "model": self.model, "messages": messages, "max_completion_tokens": 4096,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "LibrarianOutput", "strict": True, "schema": schema}},
        }
        r = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=90,
                       headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return ToolResult(r.content, content, content, "inception")

    def propose(self, ctx: LibrarianContext) -> LibrarianOutput:
        user = (f"Annotated retrieval R:{ctx.reg_id}\nClaims:\n{ctx.claims_text}\n\n"
                f"Related lobby entries:\n{ctx.entries_text}\n\nRooms: {', '.join(ctx.rooms)}\n\n"
                "Return the minimal set of edit ops.")
        messages = [{"role": "system", "content": GRAMMAR_BRIEF}, {"role": "user", "content": user}]
        _, res = self.gateway.call("mercury.chat", actor="librarian",
                                   messages=messages, schema=LibrarianOutput.model_json_schema())
        return LibrarianOutput.model_validate_json(res.text)


def get_librarian(hotel, name: str | None = None) -> Librarian:
    name = name or hotel.cfg.get("librarian", "rule")
    if name.startswith("mercury"):
        return MercuryLibrarian(hotel)
    return RuleLibrarian()


def run_librarian(hotel, reg_id: int, librarian: Librarian | None = None):
    lib = librarian or get_librarian(hotel)
    ctx = build_context(hotel, reg_id)
    out = lib.propose(ctx)
    results = [hotel.route_resident_op(op, "librarian", reg_id) for op in out.ops]
    return out, results
