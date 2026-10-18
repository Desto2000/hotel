"""Librarian: proposes structured edit ops from an annotated retrieval.

Swappable behind `Librarian.propose(ctx) -> LibrarianOutput`:
  RuleLibrarian     deterministic baseline, no network (also the replay control)
  ModelLibrarian    any router profile (Mercury 2.5 default) with schema-constrained
                    JSON; logged through the gateway as a `model` row.

Ops never apply directly: they go through Hotel.route_resident_op (policy:
rate cap, log-only appends, deterministic close check, review queue).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from .grammar import Lobby
from .gateway import load_annotation
from .llm import LLM
from .models import (
    AppendOp, Annotation, CloseOp, ContestOp, LibrarianOutput, RefOp,
)

GRAMMAR_BRIEF = """You are the librarian of a personal notes file (the lobby). Propose the smallest set of edit operations that files one annotated web retrieval into the lobby. Return exactly one JSON object matching the schema: {"rationale": str, "ops": [...]}. Zero ops is a good answer when nothing needs filing.

The lobby: entries have ids like L0007, a room (topic), a type (monologue | bounty | goal | reasoning | idea | log) and a status: [ ] open, [x] closed, [~] superseded, [!] contested, [.] static.

You are a RESIDENT. Allowed ops:
- {"op":"ref","id":"L0007","ref":"R:12"} — cite the retrieval on an entry it is about. Most retrievals need only this.
- {"op":"tag","id":..,"tag":"lowercase-slug"} and {"op":"link","id":..,"target":"L0003"}
- {"op":"append","room":..,"type":"log","body":..,"refs":[..],"tags":[..]} — type MUST be "log". Never write goals, ideas, beliefs or reasoning.
- {"op":"close","id":"L0005","closed_by":"R:12"} — only for an open bounty that a "supports" claim in the annotation directly answers.
- {"op":"contest","id":..,"reason":..} when a claim contradicts an open bounty/goal/idea.
- supersede, reopen and promote exist but are rarely right for you.

How your output is checked (do not try to work around it): a validator applies each op to the file and rejects anything that breaks the grammar; non-log appends are rejected; close/contest/supersede go to a human review queue unless mechanically provable; there is an hourly rate cap. Body text: plain, one or two sentences, no markdown. Use only ids that appear in the context."""


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
    data = load_annotation(ann_row, hotel.blobs)
    if data is None:
        raise ValueError(f"annotation of R:{reg_id} was forgotten")
    ann = Annotation.model_validate(data)
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


class ModelLibrarian:
    """Any OpenAI-compatible model via the router (Mercury 2.5 by default).
    Output is schema-constrained JSON, then validated locally with Pydantic; one
    repair turn feeds the validation error back (feedback loop, not a crash)."""

    def __init__(self, hotel, profile: str | None = None, gateway=None, transport=None):
        self.llm = LLM(hotel, profile, role="librarian", gateway=gateway, transport=transport)
        self.mode = hotel.cfg.get("librarian_output_mode", "json_schema")  # or json_object
        self.name = f"model:{self.llm.name}"

    def propose(self, ctx: LibrarianContext) -> LibrarianOutput:
        user = (f"Annotated retrieval R:{ctx.reg_id}\nClaims:\n{ctx.claims_text}\n\n"
                f"Related lobby entries:\n{ctx.entries_text}\n\nRooms: {', '.join(ctx.rooms)}\n\n"
                "Return the JSON object now.")
        messages = [{"role": "system", "content": GRAMMAR_BRIEF}, {"role": "user", "content": user}]
        schema = {"name": "LibrarianOutput", "strict": False, "schema": LibrarianOutput.model_json_schema()}
        last_err = None
        for _ in range(2):
            if self.mode == "json_schema":
                _, msg = self.llm.chat(messages, json_schema=schema, max_tokens=2048, actor="librarian")
            else:
                _, msg = self.llm.chat(messages, max_tokens=2048, actor="librarian")
            content = (msg.get("content") or "").strip()
            content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
            try:
                return LibrarianOutput.model_validate_json(content)
            except Exception as ex:
                last_err = ex
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": f"That JSON failed validation: {str(ex)[:800]}\nReturn a corrected JSON object only."}]
        raise ValueError(f"librarian output invalid after repair: {last_err}")


def get_librarian(hotel, name: str | None = None) -> Librarian:
    name = name or hotel.cfg.get("librarian", "rule")
    if name == "rule":
        return RuleLibrarian()
    # "model" -> default llm profile; "mercury" / "groq" / any profile name -> that profile
    return ModelLibrarian(hotel, None if name == "model" else name)


def run_librarian(hotel, reg_id: int, librarian: Librarian | None = None):
    lib = librarian or get_librarian(hotel)
    ctx = build_context(hotel, reg_id)
    out = lib.propose(ctx)
    results = [hotel.route_resident_op(op, "librarian", reg_id) for op in out.ops]
    return out, results
