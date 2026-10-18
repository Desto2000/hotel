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

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .blobs import BlobStore
from .grammar import ID_RE
from .models import Annotation, Pointer
from .reg import Reg, RegEntry

# Inference itself is NOT gated: an agent that just searched needs an inference
# turn to write the annotation. The gate blocks the next *tool call* / output.
GATED_KINDS = {"retrieval", "output"}


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
    meta: dict | None = None  # small structured facts merged into the Reg payload (usage, cost)


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

    def _gate(self, what: str, actor: str, kind: str, force: bool = False) -> None:
        if kind not in GATED_KINDS and not force:
            return
        pend = self.pending()
        if pend:
            self.reg.append(
                self.session_id, "blocked", actor, tool=what, status="blocked",
                payload={"pending": [p.id for p in pend]},
            )
            raise GateBlocked(pend)

    def _args_payload(self, args: dict) -> dict:
        """Arguments (queries, prompts) are content: store them as a blob so they can be
        forgotten; the row keeps only the hash and the argument names."""
        enc = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        return {"args_blob": self.blobs.put_text(enc), "arg_keys": sorted(args)}

    # ------------------------------------------------------------- calls
    def call(self, name: str, actor: str = "model", *, gate_all: bool = False, **args) -> tuple[RegEntry, ToolResult]:
        """gate_all=True: block even ungated kinds (agents: every tool call waits for annotation)."""
        spec = self.tools[name]
        self._gate(name, actor, spec.kind, force=gate_all)
        t0 = time.perf_counter()
        try:
            res = spec.fn(**args)
        except Exception as ex:  # failures are logged too, and need no annotation
            self.reg.append(
                self.session_id, spec.kind, actor, tool=name, provider=spec.provider,
                payload={**self._args_payload(args), "ms": int((time.perf_counter() - t0) * 1000)},
                status="error", error=f"{type(ex).__name__}: {ex}"[:2000],
            )
            raise
        ms = int((time.perf_counter() - t0) * 1000)
        rh = self.blobs.put(res.raw)
        th = self.blobs.put_text(res.text)
        entry = self.reg.append(
            self.session_id, spec.kind, actor, tool=name, provider=res.provider or spec.provider,
            payload={**self._args_payload(args), "ms": ms, **(res.meta or {})}, result_hash=rh, text_hash=th,
        )
        return entry, res

    def output(self, text: str, actor: str = "model", ref_id: int | None = None) -> RegEntry:
        self._gate("output", actor, "output")
        th = self.blobs.put_text(text)
        return self.reg.append(self.session_id, "output", actor, text_hash=th, ref_id=ref_id)

    def log(self, kind: str, actor: str, **kw) -> RegEntry:
        return self.reg.append(self.session_id, kind, actor, **kw)

    # ------------------------------------------------------------- annotate
    def annotate(self, ann: Annotation, actor: str = "user") -> RegEntry:
        errs = validate_annotation(ann, self.reg, self.blobs, self.lobby_ids())
        # quotes are retrieval content -> blob (forgettable); row keeps hash + shape
        h = self.blobs.put_text(json.dumps(ann.model_dump(mode="json"), sort_keys=True, ensure_ascii=False))
        payload = {"annotation_blob": h, "claims": len(ann.claims),
                   "relations": sorted({c.relation for c in ann.claims})}
        if errs:
            self.reg.append(self.session_id, "annotation", actor, ref_id=ann.target, status="error",
                            payload=payload, error="; ".join(errs)[:2000])
            raise AnnotationRejected(errs)
        return self.reg.append(self.session_id, "annotation", actor, ref_id=ann.target, payload=payload)


def load_annotation(row: RegEntry | None, blobs: BlobStore) -> dict | None:
    """Annotation dict for a Reg annotation row; None if absent or forgotten.
    Rows from before blob-storage carry the annotation inline."""
    if row is None or not row.payload:
        return None
    if "annotation_blob" in row.payload:
        try:
            return json.loads(blobs.text(row.payload["annotation_blob"]))
        except FileNotFoundError:
            return None
    return row.payload if "claims" in row.payload else None


def load_args(row: RegEntry, blobs: BlobStore) -> dict | None:
    p = row.payload or {}
    if "args_blob" in p:
        try:
            return json.loads(blobs.text(p["args_blob"]))
        except FileNotFoundError:
            return None
    return p.get("args")


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
    try:
        text = blobs.text(target.text_hash)
    except FileNotFoundError:
        return [f"R:{ann.target} was forgotten (blob redacted); annotate with claims=[] is not possible either"]
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
    """Locate quote in the stored text: exact, then whitespace-insensitive, then
    tolerant of markdown emphasis (models drop `**bold**` / `_x_` / `code` marks
    when quoting). Always returns the span of the *stored* text, so the pointer's
    quote is exact and re-verifiable."""
    quote = quote.strip()
    if not quote:
        return None
    i = text.find(quote)
    if i >= 0:
        return i, i + len(quote)
    words = quote.split()
    pat = r"\s+".join(re.escape(w) for w in words)
    m = re.search(pat, text) or re.search(pat, text, flags=re.IGNORECASE)
    if not m:
        toks = [w.strip("*_`") for w in words]
        toks = [t for t in toks if t]
        if toks:
            mk = r"[*_`]*"
            pat = mk + (mk + r"\s+" + mk).join(re.escape(t) for t in toks) + mk
            m = re.search(pat, text) or re.search(pat, text, flags=re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def make_pointer(text: str, text_hash: str, quote: str) -> Pointer | None:
    span = find_quote(text, quote)
    if span is None:
        return None
    s, e = span
    return Pointer(blob=text_hash, start=s, end=e, quote=text[s:e])
