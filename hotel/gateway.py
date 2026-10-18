"""Harness gateway + annotation gate.

Every tool is wrapped. On call the gateway:
  1. refuses (and logs a `blocked` row) if any successful retrieval is still
     unannotated and the tool kind is gated;
  2. runs the tool;
  3. stores raw bytes and a canonical text rendering by hash;
  4. appends the Reg row;
  5. only then returns the result to the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .blobs import BlobStore
from .grammar import ID_RE
from .models import Annotation, Pointer
from .reg import Reg, RegEntry

GATED_KINDS = {"retrieval", "model", "output"}


class GateBlocked(Exception):
    def __init__(self, pending: list[RegEntry]):
        self.pending = pending
        ids = ", ".join(p.rid for p in pending)
        super().__init__(f"annotation gate: annotate {ids} before the next call")


class AnnotationRejected(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class ToolResult:
    raw: bytes  # exact provider bytes
    text: str  # canonical text rendering — annotation pointers index into this
    data: Any = None  # parsed structure for the caller
    provider: str | None = None


@dataclass
class ToolSpec:
    name: str
    kind: str  # retrieval | internal | model
    provider: str
    fn: Callable[..., ToolResult]


@dataclass
class Gateway:
    reg: Reg
    blobs: BlobStore
    session_id: str
    lobby_ids: Callable[[], set[str]]
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    # ------------------------------------------------------------- gate
    def pending(self) -> list[RegEntry]:
        return self.reg.unannotated_retrievals()

    def _gate(self, what: str, actor: str, kind: str) -> None:
        if kind not in GATED_KINDS:
            return
        pend = self.pending()
        if pend:
            self.reg.append(
                self.session_id, "blocked", actor, tool=what, status="blocked",
                payload={"pending": [p.id for p in pend]},
            )
            raise GateBlocked(pend)

    # ------------------------------------------------------------- calls
    def call(self, name: str, actor: str = "model", **args) -> tuple[RegEntry, ToolResult]:
        spec = self.tools[name]
        self._gate(name, actor, spec.kind)
        try:
            res = spec.fn(**args)
        except Exception as ex:  # failures are logged too, and need no annotation
            self.reg.append(
                self.session_id, spec.kind, actor, tool=name, provider=spec.provider,
                payload={"args": args}, status="error", error=f"{type(ex).__name__}: {ex}",
            )
            raise
        rh = self.blobs.put(res.raw)
        th = self.blobs.put_text(res.text)
        entry = self.reg.append(
            self.session_id, spec.kind, actor, tool=name, provider=res.provider or spec.provider,
            payload={"args": args}, result_hash=rh, text_hash=th,
        )
        return entry, res

    def output(self, text: str, actor: str = "model") -> RegEntry:
        self._gate("output", actor, "output")
        th = self.blobs.put_text(text)
        return self.reg.append(self.session_id, "output", actor, text_hash=th)

    def log(self, kind: str, actor: str, **kw) -> RegEntry:
        return self.reg.append(self.session_id, kind, actor, **kw)

    # ------------------------------------------------------------- annotate
    def annotate(self, ann: Annotation, actor: str = "user") -> RegEntry:
        errs = validate_annotation(ann, self.reg, self.blobs, self.lobby_ids())
        if errs:
            self.reg.append(
                self.session_id, "annotation", actor, ref_id=ann.target, status="error",
                payload=ann.model_dump(mode="json"), error="; ".join(errs),
            )
            raise AnnotationRejected(errs)
        return self.reg.append(
            self.session_id, "annotation", actor, ref_id=ann.target, payload=ann.model_dump(mode="json")
        )


def _ref_ok(ref: str, lobby_ids: set[str], reg: Reg) -> bool:
    head = ref.split("#")[0]
    if ID_RE.match(head):
        return head in lobby_ids
    if head.startswith("R:") and head[2:].isdigit():
        return reg.get(int(head[2:])) is not None
    return bool(re.match(r"^RAG:\d+$", head))


def validate_annotation(ann: Annotation, reg: Reg, blobs: BlobStore, lobby_ids: set[str]) -> list[str]:
    errs: list[str] = []
    target = reg.get(ann.target)
    if target is None or target.kind != "retrieval" or target.status != "ok":
        return [f"R:{ann.target} is not a successful retrieval"]
    if reg.annotation_for(ann.target):
        return [f"R:{ann.target} is already annotated (annotations are immutable)"]
    text = blobs.text(target.text_hash)
    if not ann.claims and len(ann.caused.strip()) < 3:
        errs.append("an empty annotation must still say what the retrieval caused (e.g. 'nothing: off-topic')")
    for i, c in enumerate(ann.claims, 1):
        p = c.pointer
        if p.blob != target.text_hash:
            errs.append(f"claim {i}: pointer blob is not R:{ann.target}'s text blob")
        elif not (0 <= p.start < p.end <= len(text)):
            errs.append(f"claim {i}: span {p.start}:{p.end} out of range")
        elif text[p.start:p.end] != p.quote:
            errs.append(f"claim {i}: quote does not match stored data at {p.start}:{p.end}")
        if c.relation in ("supports", "contradicts") and not c.lobby_refs:
            errs.append(f"claim {i}: '{c.relation}' needs at least one lobby ref")
        for r in c.lobby_refs:
            if not _ref_ok(r, lobby_ids, reg):
                errs.append(f"claim {i}: lobby ref {r} does not exist")
    for r in ann.caused_refs:
        if not _ref_ok(r, lobby_ids, reg):
            errs.append(f"caused_refs: {r} does not exist")
    return errs


def find_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Locate quote in the stored text: exact first, then whitespace-insensitive.
    Returns the span of the *stored* text so the pointer quote is exact."""
    quote = quote.strip()
    if not quote:
        return None
    i = text.find(quote)
    if i >= 0:
        return i, i + len(quote)
    words = quote.split()
    pat = r"\s+".join(re.escape(w) for w in words)
    m = re.search(pat, text) or re.search(pat, text, flags=re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def make_pointer(text: str, text_hash: str, quote: str) -> Pointer | None:
    span = find_quote(text, quote)
    if span is None:
        return None
    s, e = span
    return Pointer(blob=text_hash, start=s, end=e, quote=text[s:e])
