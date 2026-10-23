"""Hotel: wires Reg, blobs, gateway, lobby store, RAG, derived index, policy.

All lobby writes go through `Hotel.apply()`:
    ops -> pure text application -> validate_transition -> write file
        -> git commit -> Reg lobby_edit row -> rebuild derived layers
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .blobs import BlobStore
from .config import Paths, load_config, save_config, DEFAULT_CONFIG
from .derived import Derived
from .events import EventLog
from .gateway import Gateway, ToolSpec, claim_ref, load_annotation
from .grammar import empty_lobby, parse
from .locking import atomic_write_text, file_lock
from .models import (
    AppendOp, CloseOp, ContestOp, LinkOp, MoveOp, PromoteOp, ReopenOp, SupersedeOp, TagOp, op_dump, op_load,
)
from .ops import OpError, apply_op
from .concierge import labels
from .concierge.embed import make_embedder
from .concierge.index import VectorIndex
from .rag import Rag, embed_text, importable_files, read_document
from .reg import Reg, utcnow
from .search import ExaClient, ParallelClient, make_tools
from .validator import validate_transition, validate_text


class ValidationFailed(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors[:6]))


class Oracle:
    def __init__(self, reg: Reg):
        self.reg = reg

    def reg_exists(self, rid: int) -> bool:
        return self.reg.get(rid) is not None

    def is_annotated_retrieval(self, rid: int) -> bool:
        e = self.reg.get(rid)
        return bool(e and e.kind == "retrieval" and e.status == "ok" and self.reg.annotation_for(rid))


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")


def init_home(root: Path) -> Paths:
    paths = Paths(root.resolve())
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.blobs.mkdir(exist_ok=True)
    if not paths.config.exists():
        save_config(paths, DEFAULT_CONFIG)
    if not paths.lobby.exists():
        paths.lobby.write_text(empty_lobby(), encoding="utf-8", newline="\n")
    (root / ".gitignore").write_text(".hotel/\nlobby.directory.txt\n", encoding="utf-8")
    (root / ".gitattributes").write_text("lobby.txt text eol=lf\n", encoding="utf-8")
    if not (root / ".git").exists():
        git(root, "init", "-q")
    git(root, "add", "lobby.txt", ".gitignore", ".gitattributes")
    _commit(root, "hotel: init lobby")
    return paths


def _commit(root: Path, msg: str) -> str | None:
    ident = []
    if not git(root, "config", "user.name").stdout.strip():
        ident = ["-c", "user.name=hotel", "-c", "user.email=hotel@localhost"]
    r = git(root, *ident, "commit", "-q", "-m", msg)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        raise RuntimeError(f"git commit failed: {r.stderr.strip() or r.stdout.strip()}")
    return git(root, "rev-parse", "HEAD").stdout.strip() or None


@dataclass
class ApplyResult:
    created: list[str]
    touched: list[str]
    commit: str | None
    reg_id: int
    skipped: list[str] = field(default_factory=list)


@dataclass
class RouteResult:
    op: object
    route: str  # applied | queued | rejected | noop
    reason: str
    detail: str = ""


class Hotel:
    def __init__(self, paths: Paths, session_id: str | None = None, new_session: bool = False,
                 label: str = "tui", source: str | None = None):
        self.paths = paths
        self.cfg = load_config(paths)
        self.blobs = BlobStore(paths.blobs)
        self.events = EventLog(paths.log, source or label)
        self.reg = Reg(paths.reg_db, self.events)
        emb = make_embedder(self.cfg)
        self.index = VectorIndex(paths.vectors_db, emb) if emb else None
        self.rag = Rag(paths.rag_db, self.blobs, self.index)
        self.derived = Derived(paths.derived_db, self.index)
        self.oracle = Oracle(self.reg)
        cur_file = paths.state / "session"
        if session_id is None and not new_session and cur_file.exists():
            session_id = cur_file.read_text().strip() or None
        if session_id is None or self.reg.session(session_id) is None:
            snap = self.blobs.put_text(self.text())
            session_id = self.reg.start_session(label, snap)
            if label == "tui":  # other front ends (mcp) leave the TUI's current session alone
                cur_file.write_text(session_id)
        self.session_id = session_id
        self.gateway = Gateway(self.reg, self.blobs, session_id, self.lobby_ids, rag_exists=self.rag.exists)
        exa = ExaClient(max_chars=self.cfg["exa_max_chars"])
        fns = make_tools(exa, self.rag, self.derived, self.cfg["exa_num_results"],
                         parallel=ParallelClient(), backend=self.cfg.get("search_backend", "exa"),
                         agent_num_results=self.cfg.get("agent_num_results", 3),
                         agent_highlight_chars=self.cfg.get("agent_highlight_chars", 700),
                         exa_agent_effort=self.cfg["exa_agent_effort"], exa_agent_max_cost=self.cfg["exa_agent_max_cost"],
                         exa_agent_timeout_s=self.cfg["exa_agent_timeout_s"])
        self.gateway.register(ToolSpec("search", "retrieval", "unified", fns["search"]))
        self.gateway.register(ToolSpec("agent.search", "retrieval", "web+rag", fns["agent.search"]))
        self.gateway.register(ToolSpec("exa.search", "retrieval", "exa", fns["exa.search"]))
        self.gateway.register(ToolSpec("exa.deep", "retrieval", "exa", fns["exa.deep"]))
        self.gateway.register(ToolSpec("exa.fetch", "retrieval", "exa", fns["exa.fetch"]))
        self.gateway.register(ToolSpec("lobby.search", "internal", "lobby", fns["lobby.search"]))
        self.rebuild()

    @classmethod
    def open(cls, root: Path, **kw) -> "Hotel":
        return cls(Paths(root), **kw)

    # ------------------------------------------------------------- lobby io
    def text(self) -> str:
        return self.paths.lobby.read_text(encoding="utf-8")

    def lobby(self):
        return parse(self.text())

    def claim_text(self, ref: str) -> str | None:
        """`R:12#c1` -> '[supports] <claim> — "<quote>"'; None for refs that are not claim refs."""
        cr = claim_ref(ref, self.reg, self.blobs)
        if cr is None:
            return None
        c = cr[2]
        if c is None:
            return f"(claim c{cr[1]} of R:{cr[0]} is gone: forgotten or never recorded)"
        return f"[{c['relation']}] {c['text']} — \"{' '.join(c['pointer']['quote'].split())[:300]}\""

    def read_entries(self, ids: list[str]) -> str:
        """Entries as their lobby lines, each claim ref followed by the claim and the quote it
        points at, so a reader gets the relevant section instead of a whole retrieval."""
        lob = self.lobby()
        blocks = []
        for i in ids:
            e = lob.entries.get(str(i))
            if not e:
                continue
            out = []
            for line in lob.lines[e.start:e.end + 1]:
                out.append(line)
                k, _, v = line.strip().removeprefix("+ ").partition(": ")
                if k in ("ref", "closed-by") and (note := self.claim_text(v.strip())):
                    out.append("      " + note)
            blocks.append("\n".join(out))
        return "\n\n".join(blocks) or "no such entries"

    def lobby_ids(self) -> set[str]:
        try:
            return set(self.lobby().entries)
        except Exception:
            return set()

    def rebuild(self) -> list[str]:
        _, errs = validate_text(self.text(), self.oracle)
        if errs:
            return errs
        self.derived.rebuild(self.text(), self.paths.directory)
        return []

    def head_text(self) -> str | None:
        r = git(self.paths.root, "show", "HEAD:lobby.txt")
        return r.stdout if r.returncode == 0 else None

    def check(self) -> list[str]:
        """Validate working-tree lobby against git HEAD (catches bad hand edits)."""
        head = self.head_text()
        cur = self.text()
        if head is None:
            return validate_text(cur, self.oracle)[1]
        return validate_transition(head, cur, self.oracle) if head != cur else validate_text(cur, self.oracle)[1]

    def sync_hand_edits(self) -> list[str]:
        """If the user edited lobby.txt by hand, validate and commit it as a user edit."""
        with file_lock(self.paths.state / "lobby.lock"):
            return self._sync_hand_edits_locked()

    def _sync_hand_edits_locked(self) -> list[str]:
        head = self.head_text()
        cur = self.text()
        if head is None or head == cur:
            return []
        errs = validate_transition(head, cur, self.oracle)
        if errs:
            return errs
        git(self.paths.root, "add", "lobby.txt")
        commit = _commit(self.paths.root, "hotel: hand edit (user)")
        self.gateway.log("lobby_edit", "user", payload={
            "ops": [], "hand_edit": True, "origin": "user",
            "before": self.blobs.put_text(head), "after": self.blobs.put_text(cur), "commit": commit,
        })
        self.rebuild()
        return []

    # ------------------------------------------------------------- apply
    def apply(self, ops: list, *, actor: str = "user", origin: str = "user",
              cause: int | None = None) -> ApplyResult:
        """Read -> apply -> validate -> write -> commit, under an exclusive cross-process
        lock. Uncommitted hand edits are validated and committed first, so a write can
        never silently absorb (or clobber) an editor's change."""
        with file_lock(self.paths.state / "lobby.lock"):
            errs = self._sync_hand_edits_locked()
            if errs:
                raise ValidationFailed(["uncommitted hand edit to lobby.txt is invalid: " + e for e in errs])
            return self._apply_locked(ops, actor=actor, origin=origin, cause=cause)

    def _apply_locked(self, ops: list, *, actor: str, origin: str, cause: int | None) -> ApplyResult:
        old = self.text()
        ts = utcnow()
        text, created, touched, op_ids = old, [], [], []
        promotes: list[tuple[int, PromoteOp]] = []
        for op in ops:
            rag_id = None
            if isinstance(op, PromoteOp):
                rag_id = self.rag.next_id() + len(promotes)
            res = apply_op(text, op, origin=origin, ts=ts, rag_id=rag_id,
                           revalidate_days=self.cfg.get("bounty_revalidate_days"))
            if res.noop:
                op_ids.append([])
                continue
            text = res.text
            created += res.created
            touched += res.touched
            op_ids.append(res.created)
            if rag_id is not None:
                promotes.append((rag_id, op))
        if text == old:
            raise OpError("no change")
        errs = validate_transition(old, text, self.oracle)
        if errs:
            raise ValidationFailed(errs)
        atomic_write_text(self.paths.lobby, text)
        for rag_id, op in promotes:
            self.rag.add("promotion", "lobby-librarian", f"promoted {op.id}", op.text, doc_id=rag_id)
        git(self.paths.root, "add", "lobby.txt")
        summary = ", ".join(f"{o.op} {getattr(o, 'id', getattr(o, 'room', ''))}" for o in ops)
        commit = _commit(self.paths.root, f"lobby[{actor}/{origin}]: {summary}")
        entry = self.gateway.log("lobby_edit", actor, ref_id=cause, payload={
            "ops": [op_dump(o) for o in ops], "created": op_ids, "origin": origin, "ts": ts,
            "revalidate_days": self.cfg.get("bounty_revalidate_days"),
            "before": self.blobs.put_text(old), "after": self.blobs.put_text(text), "commit": commit,
            "promoted": [r for r, _ in promotes],
        })
        self.rebuild()
        return ApplyResult(created, touched, commit, entry.id)

    # ------------------------------------------------------------- residents
    def close_is_deterministic(self, op: CloseOp) -> tuple[bool, str]:
        lob = self.lobby()
        e = lob.entries.get(op.id)
        if not e or e.sigil not in (" ", "!"):
            return False, f"{op.id} is not open"
        if not op.closed_by.startswith("R:"):
            return False, "closed_by must be a Reg retrieval"
        rid = int(op.closed_by[2:])
        ann_row = self.reg.annotation_for(rid)
        if not ann_row:
            return False, f"{op.closed_by} is not annotated"
        if ann_row.actor != "user":
            # A model's claim that a source answers the question is a judgment, not a
            # mechanical fact; closing on it is meaning-changing -> review.
            return False, f"annotation of {op.closed_by} was written by {ann_row.actor}, not the user"
        ann = load_annotation(ann_row, self.blobs)
        if ann is None:
            return False, f"annotation of {op.closed_by} was forgotten"
        for c in ann["claims"]:
            if c["relation"] == "supports" and op.id in c["lobby_refs"]:
                return True, f"annotation of {op.closed_by} has a supporting claim on {op.id}"
        return False, f"no supporting claim on {op.id} in annotation of {op.closed_by}"

    def route_resident_op(self, op, actor: str, cause: int | None, review: str | None = None) -> RouteResult:
        """review: queue the op for the user with this reason even when it is structural
        (a model's judgment that an entry is relevant, say)."""
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.reg.count_recent("lobby_edit", actor, since) >= self.cfg["resident_rate_cap_per_hour"]:
            return RouteResult(op, "rejected", "rate cap reached")
        if isinstance(op, AppendOp) and op.type != "log":
            return RouteResult(op, "rejected", f"residents never author new {op.type} entries")
        meaning_changing = isinstance(op, (SupersedeOp, ContestOp, ReopenOp, PromoteOp, MoveOp))
        if isinstance(op, CloseOp):
            ok, why = self.close_is_deterministic(op)
            meaning_changing, reason = (not ok), why
        else:
            reason = "meaning-changing: needs review" if meaning_changing else "structural"
        if review:
            meaning_changing, reason = True, review
        if meaning_changing:
            r = self.gateway.log("review", actor, ref_id=cause, payload={"op": op_dump(op), "reason": reason})
            return RouteResult(op, "queued", reason, f"review R:{r.id}")
        try:
            res = self.apply([op], actor=actor, origin="resident", cause=cause)
        except OpError as ex:
            return RouteResult(op, "noop", str(ex))
        except ValidationFailed as ex:
            return RouteResult(op, "rejected", "validator: " + "; ".join(ex.errors[:3]))
        return RouteResult(op, "applied", reason, f"commit {res.commit[:8] if res.commit else '-'}")

    def decide(self, review_id: int, approve: bool, actor: str = "user") -> RouteResult:
        rv = self.reg.get(review_id)
        if not rv or rv.kind != "review":
            raise ValueError(f"R:{review_id} is not a review item")
        if review_id not in {p.id for p in self.reg.pending_reviews()}:
            raise ValueError(f"R:{review_id} already decided")
        op = op_load(rv.payload["op"])
        if not approve:
            self.gateway.log("decision", actor, ref_id=review_id, payload={"verdict": "reject"})
            return RouteResult(op, "rejected", "rejected by user")
        try:
            res = self.apply([op], actor=actor, origin="approved", cause=review_id)
        except (OpError, ValidationFailed) as ex:
            self.gateway.log("decision", actor, ref_id=review_id, payload={"verdict": "approve-failed", "error": str(ex)})
            return RouteResult(op, "rejected", f"approved but failed to apply: {ex}")
        self.gateway.log("decision", actor, ref_id=review_id, payload={"verdict": "approve", "lobby_edit": res.reg_id})
        return RouteResult(op, "applied", "approved", f"commit {res.commit[:8] if res.commit else '-'}")

    # ------------------------------------------------------------- custodian
    def custodian(self, today: str | None = None) -> list[RouteResult]:
        """Resident housekeeping (deterministic, no model): closed bounties past their
        `revalidate` date get tagged `stale` plus a log entry. It never reopens or
        re-answers anything — that is a meaning change and stays with the user."""
        today = today or utcnow()[:10]
        out = []
        for e in self.lobby().entries.values():
            due = e.metas("revalidate")
            if e.sigil == "x" and due and max(due) <= today and "stale" not in e.metas("tag"):
                out.append(self.route_resident_op(TagOp(op="tag", id=e.id, tag="stale"), "custodian", None))
                out.append(self.route_resident_op(AppendOp(
                    op="append", room=e.room, type="log", refs=[e.id], tags=["stale"],
                    body=f"{e.id}: evidence due for revalidation since {max(due)}. "
                         f"Re-run /research {e.id}; supersede the answer if it changed."), "custodian", None))
        return out

    # ------------------------------------------------------------- forgetting
    def forget(self, rid: int, reason: str, actor: str = "user") -> dict:
        """Delete the content behind a Reg row (result, text, args, and for a
        retrieval its annotation's quotes). Rows stay — immutability and the hash
        chain hold — but the bytes are gone. Lobby text is NOT covered: it lives
        in git history and needs a history rewrite (git filter-repo)."""
        row = self.reg.get(rid)
        if row is None:
            raise ValueError(f"no R:{rid}")
        if not reason.strip():
            raise ValueError("a reason is required (it is logged; the content is not)")
        hashes = [h for h in (row.result_hash, row.text_hash) if h]
        p = row.payload or {}
        hashes += [p[k] for k in ("args_blob", "annotation_blob") if isinstance(p.get(k), str)]
        if row.kind == "retrieval":
            for a in self.reg.list(kind="annotation", limit=100000):
                if a.ref_id == rid and a.payload and a.payload.get("annotation_blob"):
                    hashes.append(a.payload["annotation_blob"])
        hashes = sorted(set(hashes))
        deleted = [h for h in hashes if self.blobs.delete(h)]
        labels.invalidate(self)
        self.gateway.log("redaction", actor, ref_id=rid, payload={"blobs": hashes, "deleted": len(deleted),
                                                                  "reason": reason})
        return {"target": rid, "blobs": hashes, "deleted": len(deleted)}

    # ------------------------------------------------------------- RAG
    def rag_import(self, paths: list[Path], title: str | None = None, actor: str = "user",
                   progress=None) -> list[tuple[Path, int, bool]]:
        """Import files or directories. Holds the lobby lock, like promotion, so a RAG id
        reserved by a concurrent /promote can never be taken. Returns (file, id, new)."""
        out = []
        for f in importable_files(paths):
            doc_title, text = read_document(f)
            if not text.strip():
                continue
            with file_lock(self.paths.state / "lobby.lock"):
                before = self.rag.next_id()
                did = self.rag.add("import", str(f.resolve()), title or doc_title, text)
            new = did >= before
            if new:
                d = self.rag.get(did)
                self.gateway.log("internal", actor, tool="rag.import", provider="rag",
                                 payload={"rag_id": did, "blob": d["blob"], "chars": d["chars"],
                                          **self.gateway._args_payload({"file": str(f)})})
            out.append((f, did, new))
            if progress:
                progress(f, did, new)
        if self.index is not None:  # embed now, so the first search is not the one that pays for it
            self.index.ensure([embed_text(c) for c in self.rag.chunks()])
        return out

    def forget_rag(self, doc_id: int, reason: str, actor: str = "user") -> dict:
        """Delete an imported or promoted document's text, chunks and vectors; the id stays."""
        if not reason.strip():
            raise ValueError("a reason is required (it is logged; the content is not)")
        info = self.rag.forget(doc_id)
        labels.invalidate(self)
        self.gateway.log("redaction", actor, payload={"rag": doc_id, "blobs": [info["blob"]] if info["blob"] else [],
                                                      "deleted": int(info["deleted"]), "chunks": info["chunks"],
                                                      "reason": reason})
        return info

    # ------------------------------------------------------------- agent outputs
    def accept_output(self, output_id: int, room: str | None = None) -> ApplyResult:
        """User keeps a chat agent's answer: it becomes a user-approved model draft
        (origin=approved) citing the output row and every retrieval the run made."""
        out = self.reg.get(output_id)
        if not out or out.kind != "output":
            raise ValueError(f"R:{output_id} is not an output")
        run = next((r for r in self.reg.list(kind="internal", limit=2000)
                    if r.tool == "agent.finish" and r.payload.get("output") == output_id), None)
        bounty = run.payload["bounty"] if run else None
        lob = self.lobby()
        room = room or (lob.entries[bounty].room if bounty in lob.entries else self.cfg.get("default_room", "inbox"))
        refs = [f"R:{output_id}"] + [f"R:{r}" for r in (run.payload["retrievals"] if run else [])]
        op = AppendOp(op="append", room=room, type="reasoning", body=self.blobs.text(out.text_hash),
                      refs=refs, tags=["research"])
        ops = [op]
        res = self.apply(ops, actor="user", origin="approved", cause=output_id)
        if bounty in lob.entries:
            self.apply([LinkOp(op="link", id=bounty, target=res.created[0])], actor="user", origin="user",
                       cause=output_id)
        return res
