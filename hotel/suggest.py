"""Next-edit ghost text in the composer: Mercury Edit, fed by local retrieval.

While you type a note, the draft is the editable region of a next-edit request
(https://docs.inceptionlabs.ai/capabilities/next-edit). Its context slots are
filled locally, in milliseconds, before the request leaves the machine:

  recently_viewed_code_snippets   top lobby entries + RAG chunks for the draft
                                  (hybrid search: bm25 + local embeddings, RRF)
  current_file_content            the current room's latest entries, then the draft
  edit_diff_history               the notes you wrote this session, as diffs

Only continuations of the draft are shown (Tab / → accepts): the lobby is
append-only, so a suggestion can complete what you are writing, never rewrite
what is committed. Context is your own data only — no web search — so the
annotation gate is untouched.

Every request is a `model` row in the Reg (prompt and response in blobs);
each shown suggestion's fate (accepted / partial / ignored) is an `internal`
row `suggest.outcome` holding ids and lengths only. Those outcomes are
Concierge labels (see concierge/labels.py). Text you accept stays origin=user
in the lobby; the assist is recorded in the Reg, not in the entry.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from dataclasses import dataclass

from textual.suggester import Suggester

from .gateway import ToolResult, ToolSpec
from .llm import LLMError, get_profile

TOOL = "llm.mercury-edit.suggest"
FENCE = re.compile(r"```[^\n]*\n(.*?)\n?```", re.S)


@dataclass
class Shown:
    draft: str
    suggestion: str
    reg_id: int


def build_prompt(hotel, draft: str, room: str, k: int = 4, history: list[str] | None = None) -> tuple[str, list[str]]:
    """The next-edit prompt for `draft`, and the refs whose text went into it."""
    refs, snippets = [], []
    for r in hotel.derived.search(draft, k):
        refs.append(r["eid"])
        snippets.append(f"code_snippet_file_path: lobby/{r['room']}/{r['eid']} ({r['type']})\n{r['body']}")
    for c in hotel.rag.search(draft, max(1, k // 2)):
        refs.append(c["ref"])
        head = " > ".join(x for x in (c["title"], c["heading"]) if x)
        snippets.append(f"code_snippet_file_path: rag/{c['ref']} {head}\n{c['body'][:1500]}")
    viewed = "".join(f"<|recently_viewed_code_snippet|>\n{s}\n<|/recently_viewed_code_snippet|>\n" for s in snippets)
    lob = hotel.lobby()
    recent = lob.in_room(room)[-6:] if room in lob.rooms else []
    above = "\n".join(f"{e.type}: {e.text}" for e in recent)
    diffs = "".join(f"--- lobby/{room}\n+++ lobby/{room}\n@@ -0,0 +1,1 @@\n+{h}\n\n" for h in (history or [])[-5:])
    prompt = (f"<|recently_viewed_code_snippets|>\n{viewed}<|/recently_viewed_code_snippets|>\n\n"
              f"<|current_file_content|>\ncurrent_file_path: lobby/{room}.notes\n{above}\n"
              f"<|code_to_edit|>\n{draft}<|cursor|>\n<|/code_to_edit|>\n<|/current_file_content|>\n\n"
              f"<|edit_diff_history|>\n{diffs}<|/edit_diff_history|>")
    return prompt, refs


def continuation(draft: str, content: str) -> str | None:
    """Mercury Edit returns the whole editable region in a fence. Keep it only if it
    extends the draft (the composer is a single line; anything else is a rewrite)."""
    m = FENCE.search(content)
    region = (m.group(1) if m else content).replace("<|cursor|>", "")
    region = " ".join(region.split())
    if len(region) > len(draft) and region.startswith(draft):
        return region
    return None


class NextEdit(Suggester):
    """Textual suggester. Debounced; one request in flight at a time; never blocks typing."""

    def __init__(self, hotel, room_of, transport=None, debounce: float = 0.35, min_chars: int = 12):
        super().__init__(use_cache=False, case_sensitive=True)  # a cached hit would skip `shown`
        self.hotel = hotel
        self.room_of = room_of  # callable -> current room
        self.debounce = debounce
        self.min_chars = min_chars
        self.history: list[str] = []
        self.shown: Shown | None = None
        self.error: str | None = None
        self._latest = ""
        self._busy = threading.Lock()
        self.transport = transport or self._http
        hotel.gateway.register(ToolSpec(TOOL, "model", "mercury-edit", self._call))

    # ------------------------------------------------------------- model call
    def unavailable(self) -> str | None:
        """Why suggestions cannot be requested right now (checked before every request, so a
        missing key or opt-out costs nothing and logs nothing). None = available."""
        if self.transport != self._http:
            return None
        cfg = self.hotel.cfg
        if not (cfg.get("inception_training_opt_out_confirmed")
                or os.environ.get("HOTEL_INCEPTION_TRAINING_OPTOUT") == "confirmed"):
            return "Mercury training opt-out not confirmed (`hotel setup`)"
        try:
            get_profile(cfg, "mercury").key()
        except LLMError as ex:
            return str(ex)
        return None

    def _http(self, body: dict) -> bytes:
        prof = get_profile(self.hotel.cfg, "mercury")
        import inceptionai
        client = inceptionai.Inception(api_key=prof.key(), timeout=8, max_retries=0)
        return client.edit.completions.with_raw_response.create(**body).http_response.content

    def _call(self, body: dict) -> ToolResult:
        import json
        raw = self.transport(body)
        resp = json.loads(raw)
        content = resp["choices"][0]["message"]["content"] or ""
        u = resp.get("usage") or {}
        return ToolResult(raw, content, content, "inception",
                          {"model": body["model"], "tokens_in": u.get("prompt_tokens"),
                           "tokens_out": u.get("completion_tokens")})

    def request(self, draft: str) -> tuple[str | None, int | None]:
        """Synchronous: build the prompt, call Mercury Edit through the gateway."""
        prompt, refs = build_prompt(self.hotel, draft, self.room_of(), self.hotel.cfg.get("suggest_context", 4),
                                    self.history)
        body = {"model": self.hotel.cfg.get("suggest_model", "mercury-edit-2"),
                "messages": [{"role": "user", "content": prompt}], "max_tokens": 160}
        entry, res = self.hotel.gateway.call(TOOL, actor="suggest", body=body)
        return continuation(draft, res.text), entry.id

    async def get_suggestion(self, value: str) -> str | None:
        self._latest = value
        if (not self.hotel.cfg.get("suggest", True) or len(value.strip()) < self.min_chars
                or value.startswith(("/", ">"))):
            return None
        self.error = self.unavailable()
        if self.error:
            return None
        await asyncio.sleep(self.debounce)
        if value != self._latest or not self._busy.acquire(blocking=False):
            return None  # superseded by newer typing, or a request is in flight
        try:
            sugg, rid = await asyncio.to_thread(self.request, value)
        except Exception as ex:  # missing key, offline, opt-out: no ghost text, never an error dialog
            self.error = f"{type(ex).__name__}: {ex}"[:200]
            return None
        finally:
            self._busy.release()
        self.error = None
        if sugg and value == self._latest:
            self.shown = Shown(value, sugg, rid)
            return sugg
        return None

    # ------------------------------------------------------------- outcome
    def submitted(self, line: str, lobby_edit: int | None = None) -> str | None:
        """Record what happened to the last suggestion shown for this draft."""
        s, self.shown = self.shown, None
        if not line.startswith(("/", ">")):
            self.history.append(line)
        if s is None:
            return None
        if line == s.suggestion.strip():
            outcome = "accepted"
        elif line.startswith(s.draft) and os.path.commonprefix([line, s.suggestion]) != s.draft:
            outcome = "partial"
        else:
            outcome = "ignored"
        added = len(s.suggestion) - len(s.draft)
        kept = len(os.path.commonprefix([line, s.suggestion])) - len(s.draft)
        self.hotel.gateway.log("internal", "user", tool="suggest.outcome", ref_id=lobby_edit, payload={
            "suggestion": s.reg_id, "outcome": outcome, "draft_chars": len(s.draft), "suggested_chars": added,
            "kept_chars": max(0, kept), "final_chars": len(line)})
        return outcome
