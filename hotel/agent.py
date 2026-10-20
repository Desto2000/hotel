"""Research agent — a chat agent: reads the lobby, searches, never writes to it.

An LLM, a loop, and tools (no framework). One run = one task = one bounty, so
the context stays small and focused.

Every tool call goes through the gateway with gate_all=True, so after a search
the only call that succeeds is `annotate`. Rejections come back as tool
results the model must fix — the gate is the feedback loop, not a crash.

Citations are Reg ids ([R:12]): the Reg is already an append-only evidence
store with stable ids, so there's no separate citation table.

Context budget: once the transcript exceeds `budget_chars`, the oldest
*annotated* retrieval results are replaced by their annotation — the claims
are the distilled form of that retrieval.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .gateway import AnnotationRejected, GateBlocked, find_quote, make_pointer
from .llm import LLM
from .models import Annotation, Claim
from .search import clean_queries

SYSTEM = """You are the research agent of a personal note-taking harness. Your job: answer ONE open question (a bounty) from the user's lobby with web evidence. You never edit the lobby; the user decides what to keep.

Workflow
1. Call `search` with 1-3 queries that attack the question from different angles. Prefer official, primary sources.
2. Immediately call `annotate` on the retrieval you just got (its id is in the result header, e.g. R:12). The harness rejects every other call until you do.
3. Search again only for facts that are still missing or need verification. Never repeat a query.
   For a question that needs multi-hop research, comparison across many sources or an exhaustive list, call `deep_research` once instead (slow and costly; never for a simple lookup). Its pages are real sources: quote those. Its synthesis is secondhand: quote it only when no page states the point.
4. Stop as soon as the evidence answers the question. Reply with plain text and NO tool call: at most 120 words, no markdown, every factual sentence cites its retrieval inline like [R:12]. If something is unsupported, say it is unknown.

How annotate is verified (mechanically, by the harness)
- `quote` must be copied from the retrieval text; only whitespace and markdown emphasis marks (** _ `) may differ. Keep quotes to one sentence or table row or less.
- `relation`: "supports" or "contradicts" require `lobby_refs` (e.g. the bounty id); otherwise use "new" or "unrelated".
- If the retrieval was useless, annotate with "claims": [] and "caused": "nothing: <why>".
- Answer only from what retrievals explicitly state. No outside knowledge."""

TOOLS = [
    {"type": "function", "function": {
        "name": "search",
        "description": "Web (+ imported RAG) search. Returns a retrieval R:<n> whose text you must annotate before any other call.",
        "parameters": {"type": "object", "properties": {
            "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3,
                        "description": "1-3 distinct, specific search queries"}},
            "required": ["queries"]}}},
    {"type": "function", "function": {
        "name": "deep_research",
        "description": "Deep web research by Exa: iterative searches plus a grounded synthesis. Slow and costly; at most once per task. "
                       "depth 'deep' takes seconds to a minute; 'agent' runs a full research agent for minutes (list building, multi-hop questions). "
                       "Returns a retrieval R:<n> you must annotate next.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "the research task: what to find, scope, evidence required"},
            "depth": {"type": "string", "enum": ["deep", "agent"]},
            "angles": {"type": "array", "items": {"type": "string"}, "maxItems": 3,
                       "description": "optional, depth=deep only: distinct sub-questions to start from, not rephrasings"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch",
        "description": "Fetch the full text of one URL seen in a previous retrieval. Also must be annotated.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "lobby_search",
        "description": "Search the user's own lobby notes (their prior conclusions). Not external evidence.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "annotate",
        "description": "Record what a retrieval says. Required after every search/fetch.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "integer", "description": "retrieval id n from R:<n>"},
            "claims": {"type": "array", "items": {"type": "object", "properties": {
                "text": {"type": "string", "description": "the claim in your words"},
                "quote": {"type": "string", "description": "exact supporting text from the retrieval"},
                "relation": {"type": "string", "enum": ["supports", "contradicts", "new", "unrelated"]},
                "lobby_refs": {"type": "array", "items": {"type": "string"}}},
                "required": ["text", "quote", "relation", "lobby_refs"]}},
            "caused": {"type": "string", "description": "what this retrieval changed, e.g. 'answers L0005'"},
            "caused_refs": {"type": "array", "items": {"type": "string"}}},
            "required": ["target", "claims", "caused", "caused_refs"]}}},
]
ANNOTATE_ONLY = [t for t in TOOLS if t["function"]["name"] == "annotate"]


def normalize_citations(text: str) -> str:
    """【12】 / [ R:12 ] / [12] -> [R:12] (models drift on bracket style)."""
    text = re.sub(r"[【\[]\s*R?:?\s*(\d{1,6})\s*[】\]]", r"[R:\1]", text)
    return text


def quote_hint(text: str, quote: str) -> str:
    """Actionable rejection: how much of the quote matched, and what the source
    really says next — so the model can fix a stitched/paraphrased quote."""
    words = quote.split()
    best = None
    for n in range(len(words), 1, -1):
        span = find_quote(text, " ".join(words[:n]))
        if span:
            best = (n, span)
            break
    if not best:
        return f"No part of it matches; copy a sentence exactly. Your quote began: {quote[:60]!r}"
    n, (s, e) = best
    return (f"The first {n} of {len(words)} words match, then the source continues: "
            f"{text[e:e + 90]!r} — not {' '.join(words[n:n + 8])!r}. Quote one contiguous span.")


def model_annotate(hotel, args: dict, actor: str, verifier_transport=None):
    """Turn a model's `annotate` tool call into a verified annotation.
    Returns (message_for_model, reg_row_or_None, claims). Every rejection is a
    concrete instruction the model can act on."""
    target = args.get("target")
    if isinstance(target, str):
        target = int(re.sub(r"\D", "", target) or 0)
    entry = hotel.reg.get(int(target or 0))
    if entry is None or entry.kind != "retrieval":
        return (f"ERROR: R:{target} is not a retrieval. Pending: "
                + ", ".join(p.rid for p in hotel.gateway.pending()), None, [])
    text = hotel.blobs.text(entry.text_hash)
    claims, errs = [], []
    for i, c in enumerate(args.get("claims") or [], 1):
        ptr = make_pointer(text, entry.text_hash, str(c.get("quote", "")))
        if ptr is None:
            errs.append(f"claim {i}: quote is not contiguous text from R:{entry.id}. {quote_hint(text, str(c.get('quote', '')))}")
            continue
        try:
            claims.append(Claim(text=str(c.get("text", "")).strip() or "(empty)", pointer=ptr,
                                relation=c.get("relation", "new"), lobby_refs=list(c.get("lobby_refs") or [])))
        except Exception as ex:
            errs.append(f"claim {i}: {ex}")
    if not errs and claims and hotel.cfg.get("verifier"):
        from .verify import verify_claims
        verdicts = verify_claims(hotel, [{"text": c.text, "quote": c.pointer.quote} for c in claims],
                                 hotel.cfg["verifier"], transport=verifier_transport)
        for v, c in zip(verdicts, claims):
            if v.get("verdict") != "entailed":
                errs.append(f"claim {v['i']} ({c.text[:60]!r}): verifier says {v.get('verdict')} — {v.get('why', '')}. "
                            "Narrow the claim to exactly what the quote states, or drop it.")
    if errs:
        return "ERROR: annotation rejected — fix and call annotate again:\n- " + "\n- ".join(errs), None, []
    ann = Annotation(target=entry.id, claims=claims, caused=str(args.get("caused") or "").strip() or "unspecified",
                     caused_refs=list(args.get("caused_refs") or []))
    row = hotel.gateway.annotate(ann, actor=actor)
    return f"OK: annotation R:{row.id} recorded for R:{entry.id} ({len(claims)} claims).", row, claims


@dataclass
class RunResult:
    bounty: str
    output_id: int | None
    answer: str
    retrievals: list[int] = field(default_factory=list)
    annotations: list[int] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    model_ms: int = 0
    tool_ms: int = 0
    turns: int = 0

    def timings(self) -> str:
        total = max(1, self.model_ms + self.tool_ms)
        return (f"model {self.model_ms} ms ({100 * self.model_ms // total}%) · "
                f"tools {self.tool_ms} ms ({100 * self.tool_ms // total}%) · {self.turns} turns")


class ResearchAgent:
    def __init__(self, hotel, llm: LLM | None = None, *, max_turns: int = 14, max_searches: int = 4,
                 max_deep: int = 1, fanout: int = 3, budget_chars: int = 60_000, on_event=None, verifier_transport=None):
        self.h = hotel
        self.verifier_transport = verifier_transport
        self.llm = llm or LLM(hotel, role="agent")
        self.max_turns, self.max_searches, self.max_deep, self.fanout = max_turns, max_searches, max_deep, fanout
        self.budget_chars = budget_chars
        self.on_event = on_event or (lambda s: None)

    # ----------------------------------------------------------- context
    def _opening(self, bounty_id: str) -> str:
        lob = self.h.lobby()
        e = lob.entries.get(bounty_id)
        if e is None:
            raise ValueError(f"no entry {bounty_id}")
        related = [r for r in self.h.derived.search(e.text, 6) if r["eid"] != e.id]
        rel = "\n".join(f"- {r['eid']} ({r['type']}, {r['status']}): {r['body'][:200]}" for r in related) or "(none)"
        return (f"Bounty {e.id} in room '{e.room}' ({e.type}, {e.status}):\n{e.text}\n\n"
                f"Possibly related lobby entries (the user's own notes, not evidence):\n{rel}\n\n"
                f"Use lobby_refs ['{e.id}'] when a claim supports or contradicts the bounty. "
                f"You have at most {self.max_searches} searches and {self.max_deep} deep_research call.")

    def _compact(self, messages: list[dict], ann_text: dict[int, str]) -> None:
        size = sum(len(m.get("content") or "") for m in messages)
        if size <= self.budget_chars:
            return
        for m in messages:
            if size <= self.budget_chars:
                break
            rid = m.get("_rid")
            if m.get("role") == "tool" and rid in ann_text and not m.get("_compacted"):
                before = len(m["content"])
                m["content"] = f"R:{rid} (full text elided to save context; your annotation) {ann_text[rid]}"
                m["_compacted"] = True
                size -= before - len(m["content"])

    @staticmethod
    def _wire(messages: list[dict]) -> list[dict]:
        return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]

    # ----------------------------------------------------------- tools
    def _tool(self, name: str, args: dict, run: RunResult, state: dict) -> tuple[str, int | None]:
        gw = self.h.gateway
        try:
            if name == "search":
                if state["searches"] >= self.max_searches:
                    return "ERROR: search budget exhausted. Annotate pending retrievals and answer now.", None
                qs, rejected = clean_queries(args.get("queries") or [], state["seen_q"], self.fanout)
                if not qs:
                    return "ERROR: no usable queries: " + "; ".join(rejected), None
                e, res = gw.call("agent.search", actor="agent", gate_all=True, queries=qs,
                                 objective=state["objective"], skip_urls=sorted(state["urls"]))
                state["searches"] += 1
                state["urls"].update(h.url for h in res.data if h.url)
                run.retrievals.append(e.id)
                run.tool_ms += e.payload.get("ms", 0)
                note = f"\n(rejected queries: {'; '.join(rejected)})" if rejected else ""
                return f"RETRIEVAL R:{e.id} — annotate it next.{note}\n\n{res.text}", e.id
            if name == "deep_research":
                if state["deep"] >= self.max_deep:
                    return "ERROR: deep_research budget exhausted. Use search, or answer now.", None
                depth = "agent" if args.get("depth") == "agent" else "deep"
                e, res = gw.call("exa.deep", actor="agent", gate_all=True, query=str(args.get("query", "")),
                                 depth=depth, angles=list(args.get("angles") or [])[:3])
                state["deep"] += 1
                state["urls"].update(h.url for h in res.data if h.url)
                run.retrievals.append(e.id)
                run.tool_ms += e.payload.get("ms", 0)
                return f"RETRIEVAL R:{e.id} — annotate it next.\n\n{res.text}", e.id
            if name == "fetch":
                e, res = gw.call("exa.fetch", actor="agent", gate_all=True, url=str(args.get("url", "")))
                run.retrievals.append(e.id)
                run.tool_ms += e.payload.get("ms", 0)
                return f"RETRIEVAL R:{e.id} — annotate it next.\n\n{res.text}", e.id
            if name == "lobby_search":
                e, res = gw.call("lobby.search", actor="agent", gate_all=True, query=str(args.get("query", "")))
                return res.text, None
            if name == "annotate":
                return self._annotate(args, run, state), None
            return f"ERROR: unknown tool {name}", None
        except GateBlocked as ex:
            return f"ERROR: {ex}. Call annotate on it now.", None
        except AnnotationRejected as ex:
            return "ERROR: annotation rejected — fix and call annotate again:\n- " + "\n- ".join(ex.errors), None
        except Exception as ex:  # provider failures etc. — logged by the gateway; tell the model
            return f"ERROR: {type(ex).__name__}: {str(ex)[:300]}", None

    def _annotate(self, args: dict, run: RunResult, state: dict) -> str:
        msg, row, claims = model_annotate(self.h, args, "agent", self.verifier_transport)
        if row is not None:
            run.annotations.append(row.id)
            state["ann_text"][row.ref_id] = "; ".join(
                f"[{c.relation}] {c.text} — \"{c.pointer.quote}\"" for c in claims) or "(no claims)"
        return msg

    # ----------------------------------------------------------- loop
    def run(self, bounty_id: str) -> RunResult:
        pend = self.h.gateway.pending()
        if pend:
            raise GateBlocked(pend)  # don't make the agent annotate the user's retrievals
        start = self.h.gateway.log("internal", "agent", tool="agent.start",
                                   payload={"bounty": bounty_id, "model": self.llm.name})
        objective = self.h.lobby().entries[bounty_id].text if bounty_id in self.h.lobby().entries else bounty_id
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": self._opening(bounty_id)}]
        run = RunResult(bounty_id, None, "")
        state = {"searches": 0, "deep": 0, "seen_q": [], "urls": set(), "objective": objective, "ann_text": {}}
        nudges = 0
        for turn in range(self.max_turns):
            run.turns = turn + 1
            self._compact(messages, state["ann_text"])
            pending = self.h.gateway.pending()
            # The gate, expressed in the schema: while a retrieval is unannotated the
            # model is offered ONLY `annotate`, and must call it. Error text alone
            # doesn't steer weaker models (they retry blocked calls).
            if pending:
                tools, choice = ANNOTATE_ONLY, {"type": "function", "function": {"name": "annotate"}}
            elif turn >= self.max_turns - 2 or state["searches"] >= self.max_searches:
                tools, choice = None, None  # answer turn: no tools offered at all
                if not messages[-1].get("_answer_prompt"):
                    messages.append({"role": "user", "content": "Harness: search budget used. Answer now in plain text.",
                                     "_answer_prompt": True})
            else:
                tools, choice = TOOLS, "auto"
            try:
                entry, msg = self.llm.chat(self._wire(messages), tools=tools, tool_choice=choice, actor="agent")
            except Exception as ex:  # logged by the gateway as an error row
                if "tool_use_failed" in str(ex) and state.setdefault("tool_fail", 0) < 3:
                    # provider rejected the model's call server-side (e.g. a tool not offered);
                    # that's feedback for the model, not a reason to abort the run
                    state["tool_fail"] += 1
                    hint = (f"call annotate on {', '.join(p.rid for p in pending)} — it is the only tool available now"
                            if pending else "use only the tools offered, or answer in plain text")
                    messages.append({"role": "user", "content": f"Harness: your last tool call was invalid ({str(ex)[100:260]}). {hint}."})
                    continue
                run.log.append(f"gave up: model call failed: {str(ex)[:300]}")
                break
            run.model_ms += entry.payload.get("ms", 0)
            calls = msg.get("tool_calls") or []
            asst = {"role": "assistant", "content": msg.get("content") or ""}
            if calls:
                asst["tool_calls"] = calls
            messages.append(asst)
            if not calls:
                answer = normalize_citations((msg.get("content") or "").strip())
                if not answer:
                    messages.append({"role": "user", "content": "Harness: empty reply. Answer now in plain text."})
                    continue
                try:
                    out = self.h.gateway.output(answer, actor="agent", ref_id=start.id)
                except GateBlocked as ex:
                    nudges += 1
                    if nudges > 3:
                        run.log.append("gave up: model kept answering with unannotated retrievals")
                        break
                    messages.append({"role": "user", "content": f"Harness: {ex}. Annotate before answering."})
                    continue
                cited = {int(x) for x in re.findall(r"\[R:(\d+)\]", answer)}
                bad = sorted(cited - set(run.retrievals))
                if bad:
                    run.log.append(f"warning: answer cites non-run retrievals {bad}")
                run.output_id, run.answer = out.id, answer
                break
            for call in calls:
                fn = call.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError as ex:
                    result, rid = f"ERROR: arguments are not valid JSON: {ex}", None
                else:
                    result, rid = self._tool(fn.get("name", ""), args, run, state)
                self.on_event(f"{fn.get('name')}: {result[:300]}".replace(chr(10), " "))
                run.log.append(f"{fn.get('name')} -> {result.splitlines()[0][:160]}")
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result, "_rid": rid})
        self.h.gateway.log("internal", "agent", tool="agent.finish", ref_id=start.id, payload={
            "bounty": bounty_id, "output": run.output_id, "retrievals": run.retrievals,
            "annotations": run.annotations, "turns": run.turns, "model_ms": run.model_ms, "tool_ms": run.tool_ms,
        })
        return run
