"""Guest agent — works in the lobby with the user present; edits only with approval.

This is the "replace chatting with AI" loop:
  * In guest mode, every line you type is appended to the lobby as YOUR
    monologue (origin=user). Your side of the conversation IS the note.
  * The guest replies in the side panel. Its reply is a gated `output` row in
    the Reg — it never lands in the lobby by itself.
  * When the guest thinks something is worth keeping (an idea, a bounty, a
    correction, a tag, a move) it calls `propose`. Each op is dry-run against
    the validator and parked in the review queue; `/approve R:n` applies it
    with origin=approved. Rejected or ignored drafts stay in the Reg only.

One conversation = one thread; `/guest new` starts a fresh one (short context).
Same harness rules as every agent: web search is gated, annotate before
anything else, rejections come back as fixable tool results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .agent import ANNOTATE_ONLY, model_annotate
from .gateway import AnnotationRejected, GateBlocked
from .llm import LLM
from .models import EDIT_OP, op_dump
from .ops import OpError, apply_op
from .reg import utcnow
from .search import clean_queries
from .validator import validate_transition

SYSTEM = """You are a guest in the user's lobby: a single living plain-text notes file where they think. They are present and talking to you. Your job is to be a sharp thinking partner: question their reasoning, point out gaps and contradictions with what the lobby already says, and suggest what is worth writing down.

The lobby: entries like L0007 live in rooms (topics); types are monologue, bounty (open question), goal, reasoning, idea, log; status [ ] open, [x] closed, [~] superseded, [!] contested, [.] static. The user's own messages are already being appended as monologue entries — do not propose copies of what they said.

Tools
- lobby_search / read_entry: read their notes before you contradict or build on them. Cite entries by id (L0007).
- search: web evidence (1-3 queries). The harness then requires `annotate` on the retrieval before anything else; quote contiguous source text.
- propose: draft edits for the user to approve. Use it sparingly — only for things worth keeping: a crisp open question as a bounty, an idea they circled, a correction (supersede), a tag/link, a move to a better room. Each op is validated; errors come back to you to fix.

Reply style: plain text, short (<= 120 words), direct, no flattery, no markdown headers. If you proposed drafts, say so in one line with their review ids. Cite web evidence as [R:n]."""

TOOLS = [
    {"type": "function", "function": {
        "name": "lobby_search", "description": "Full-text search over the user's lobby entries.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_entry", "description": "Read lobby entries by id, with their metadata.",
        "parameters": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
                       "required": ["ids"]}}},
    {"type": "function", "function": {
        "name": "search", "description": "Web search. Returns retrieval R:<n>; you must annotate it next.",
        "parameters": {"type": "object", "properties": {
            "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3}},
            "required": ["queries"]}}},
    ANNOTATE_ONLY[0],
    {"type": "function", "function": {
        "name": "propose",
        "description": "Draft lobby edits for the user's approval. ops use the lobby edit-op schema: "
                       "append {op,room,type,body,refs,tags}; supersede {op,id,type,body,reason}; "
                       "tag {op,id,tag}; link {op,id,target}; ref {op,id,ref}; move {op,id,room,reason}; "
                       "contest/reopen {op,id,reason}.",
        "parameters": {"type": "object", "properties": {
            "ops": {"type": "array", "items": {"type": "object"}, "minItems": 1},
            "why": {"type": "string", "description": "one line: why this is worth keeping"}},
            "required": ["ops", "why"]}}},
]


def draft_ops(hotel, raw_ops: list, why: str | None, actor: str, ref_id: int | None) -> tuple[list[int], str]:
    """Model-drafted lobby edits -> review queue. Each op is schema-checked, deduplicated
    against pending drafts and dry-run against the real validator, so whatever lands in
    review is applicable. Returns (review ids, message for the model)."""
    why = str(why or "").strip() or f"{actor} suggestion"
    text = hotel.text()
    ts = utcnow()
    ids, errs = [], []
    queued = {json.dumps(r.payload.get("op"), sort_keys=True): r.id for r in hotel.reg.pending_reviews()}
    for i, raw in enumerate(raw_ops, 1):
        try:
            op = EDIT_OP.validate_python(raw)
        except Exception as ex:
            errs.append(f"op {i}: schema: {str(ex)[:240]}")
            continue
        key = json.dumps(op_dump(op), sort_keys=True)
        if key in queued:  # models re-propose; one draft per op
            errs.append(f"op {i}: already queued as R:{queued[key]} — don't propose it again")
            continue
        try:  # dry run against the real validator so the draft is applicable
            res = apply_op(text, op, origin="approved", ts=ts, rag_id=0 if op.op == "promote" else None)
            v = validate_transition(text, res.text, hotel.oracle)
            if v:
                raise OpError("; ".join(v[:2]))
        except Exception as ex:
            errs.append(f"op {i} ({raw.get('op')}): {str(ex)[:240]}")
            continue
        r = hotel.gateway.log("review", actor, ref_id=ref_id,
                              payload={"op": op_dump(op), "reason": f"{actor} draft: {why}"})
        queued[key] = r.id
        ids.append(r.id)
    out = f"queued for the user's approval: {', '.join(f'R:{i}' for i in ids)}" if ids else "nothing queued"
    if errs:
        out += "\nrejected (fix and propose again if still worth it):\n- " + "\n- ".join(errs)
    return ids, out


@dataclass
class GuestTurn:
    reply: str
    output_id: int | None
    proposals: list[int] = field(default_factory=list)  # review row ids
    retrievals: list[int] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


class Guest:
    def __init__(self, hotel, llm: LLM | None = None, *, max_steps: int = 8, max_history_chars: int = 24_000,
                 verifier_transport=None, on_event=None):
        self.h = hotel
        self.llm = llm or LLM(hotel, role="agent")
        self.max_steps = max_steps
        self.max_history_chars = max_history_chars
        self.verifier_transport = verifier_transport
        self.on_event = on_event or (lambda s: None)
        self.history: list[dict] = []
        self.thread = self.h.gateway.log("internal", "guest", tool="guest.thread", payload={"model": self.llm.name}).id
        self.seen_q: list[set[str]] = []

    # ----------------------------------------------------------- context
    def _system(self) -> str:
        directory = self.h.paths.directory.read_text(encoding="utf-8") if self.h.paths.directory.exists() else ""
        return f"{SYSTEM}\n\nCurrent lobby directory (open entries only):\n{directory[:4000]}"

    def _trim(self) -> None:
        """Short threads: drop the oldest exchanges (whole user->assistant spans) over budget."""
        while sum(len(m.get("content") or "") for m in self.history) > self.max_history_chars and len(self.history) > 2:
            cut = next((i for i in range(1, len(self.history)) if self.history[i]["role"] == "user"), len(self.history))
            del self.history[:cut]

    # ----------------------------------------------------------- tools
    def _propose(self, args: dict, turn: GuestTurn) -> str:
        ids, msg = draft_ops(self.h, args.get("ops") or [], args.get("why"), "guest", self.thread)
        turn.proposals += ids
        return msg

    def _tool(self, name: str, args: dict, turn: GuestTurn) -> str:
        gw = self.h.gateway
        try:
            if name == "lobby_search":
                _, res = gw.call("lobby.search", actor="guest", gate_all=True, query=str(args.get("query", "")))
                return res.text
            if name == "read_entry":
                gw._gate("read_entry", "guest", "internal", force=True)
                lob = self.h.lobby()
                blocks = ["\n".join(lob.lines[e.start:e.end + 1]) for i in args.get("ids") or []
                          if (e := lob.entries.get(str(i)))]
                return "\n\n".join(blocks) or "no such entries"
            if name == "search":
                qs, rejected = clean_queries(args.get("queries") or [], self.seen_q, 3)
                if not qs:
                    return "ERROR: no usable queries: " + "; ".join(rejected)
                e, res = gw.call("agent.search", actor="guest", gate_all=True, queries=qs,
                                 objective=" | ".join(qs), skip_urls=[])
                turn.retrievals.append(e.id)
                return f"RETRIEVAL R:{e.id} — annotate it next.\n\n{res.text}"
            if name == "annotate":
                msg, _, _ = model_annotate(self.h, args, "guest", self.verifier_transport)
                return msg
            if name == "propose":
                gw._gate("propose", "guest", "internal", force=True)
                return self._propose(args, turn)
            return f"ERROR: unknown tool {name}"
        except GateBlocked as ex:
            return f"ERROR: {ex}. Call annotate on it now."
        except AnnotationRejected as ex:
            return "ERROR: annotation rejected:\n- " + "\n- ".join(ex.errors)
        except Exception as ex:
            return f"ERROR: {type(ex).__name__}: {str(ex)[:300]}"

    # ----------------------------------------------------------- one exchange
    def say(self, user_text: str, note_id: str | None = None) -> GuestTurn:
        if self.h.gateway.pending():
            raise GateBlocked(self.h.gateway.pending())
        content = user_text if not note_id else f"[{note_id}, just appended to the lobby] {user_text}"
        self.history.append({"role": "user", "content": content})
        self._trim()
        turn = GuestTurn("", None)
        msgs = self.history  # tool traffic of this exchange is kept until trimmed
        for step in range(self.max_steps):
            pending = self.h.gateway.pending()
            if pending:
                tools, choice = ANNOTATE_ONLY, {"type": "function", "function": {"name": "annotate"}}
            elif step >= self.max_steps - 1:
                tools, choice = None, None
            else:
                tools, choice = TOOLS, "auto"
            try:
                entry, msg = self.llm.chat([{"role": "system", "content": self._system()}] + msgs,
                                           tools=tools, tool_choice=choice, actor="guest")
            except Exception as ex:
                if "tool_use_failed" in str(ex) and step < self.max_steps - 1:
                    msgs.append({"role": "user", "content": "Harness: your last tool call was invalid. "
                                 + ("Call annotate now." if pending else "Use only offered tools or reply in text.")})
                    continue
                turn.log.append(f"model call failed: {str(ex)[:300]}")
                break
            calls = msg.get("tool_calls") or []
            asst = {"role": "assistant", "content": msg.get("content") or ""}
            if calls:
                asst["tool_calls"] = calls
            msgs.append(asst)
            if not calls:
                reply = (msg.get("content") or "").strip()
                if not reply:
                    msgs.append({"role": "user", "content": "Harness: empty reply; answer in plain text."})
                    continue
                try:
                    out = self.h.gateway.output(reply, actor="guest", ref_id=self.thread)
                except GateBlocked as ex:
                    msgs.append({"role": "user", "content": f"Harness: {ex}. Annotate first."})
                    continue
                turn.reply, turn.output_id = reply, out.id
                break
            for c in calls:
                fn = c.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    result = self._tool(fn.get("name", ""), args, turn)
                except json.JSONDecodeError as ex:
                    result = f"ERROR: arguments are not valid JSON: {ex}"
                self.on_event(f"{fn.get('name')}: {result[:160]}".replace("\n", " "))
                turn.log.append(f"{fn.get('name')} -> {result.splitlines()[0][:160]}")
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": result})
        return turn
