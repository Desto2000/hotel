"""hotel TUI — the lobby is the product; the TUI is a cockpit over it.

Typing plain text appends a monologue entry to the current room. Everything
else is a /command. External retrievals lock the annotation gate until you
annotate them (ctrl+a).
"""

from __future__ import annotations

import json
import shlex
import threading
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Input, Label, Select, Static, TabbedContent, TabPane, Tree,
)

from .core import Hotel, ValidationFailed
from .gateway import AnnotationRejected, GateBlocked, make_pointer
from .grammar import SIGIL_NAMES
from .librarian import run_librarian
from .models import (
    Annotation, AppendOp, Claim, CloseOp, ContestOp, LinkOp, MoveOp, PromoteOp, RefOp, ReopenOp, SupersedeOp, TagOp,
)
from .ops import OpError
from .render import highlight_lobby
from .theme import lobby_styles, palettes, textual_theme

TYPE_CMDS = {"bounty": "bounty", "goal": "goal", "idea": "idea", "reason": "reasoning",
             "log": "log", "mono": "monologue"}

HELP = """[b]write[/b]   plain text → monologue in current room · /bounty /goal /idea /reason /log <text>
[b]rooms[/b]   /room <slug>
[b]search[/b]  /s <q> unified (exa+rag+lobby) · /exa <q> · /fetch <url> · /ls <q> lobby only (ungated)
[b]gate[/b]    /annotate [R:n] (ctrl+a) — required after every external retrieval
[b]edit[/b]    /close L R:n · /supersede L <text> · /contest L <why> · /reopen L <why>
        /tag L t · /link L L · /ref L R:n · /promote L
[b]guest[/b]   /guest (on) · /guest new · /guest off — your lines become notes, the guest replies, drafts → Review
        in guest mode, start a line with > to talk without writing it down (e.g. "> yes, draft it") · /talk <msg>
[b]agent[/b]   /research L#### (one bounty per run) · /accept R:n keep an answer as an approved entry
[b]house[/b]   /move L <room> [why] · /custodian (flag stale closed bounties) · /backup [dir]
[b]resident[/b] /lib [R:n] run librarian · /approve R:n · /reject R:n
[b]inspect[/b] /blob R:n · /check · /verify · /theme (ctrl+t) · /q"""


# ======================================================================== modal
class AnnotateScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, hotel: Hotel, rid: int, styles: dict):
        super().__init__()
        self.hotel, self.rid, self.st = hotel, rid, styles
        self.entry = hotel.reg.get(rid)
        self.text = hotel.blobs.text(self.entry.text_hash)
        self.claims: list[Claim] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="ann"):
            with VerticalScroll(id="ann-blob") as vs:
                vs.border_title = f"R:{self.rid} · {self.entry.tool} · {self.entry.provider} · text {self.entry.text_hash[:10]}"
                yield Static(Text(self.text), id="ann-text")
            with Vertical(id="ann-form") as f:
                f.border_title = "annotation"
                yield Label("claim (your words)")
                yield Input(placeholder="what the source asserts", id="a-claim")
                yield Label("quote (exact or whitespace-loose text from the left pane)")
                yield Input(placeholder="pointer is computed from this", id="a-quote")
                with Horizontal(classes="row"):
                    yield Select([(r, r) for r in ("new", "supports", "contradicts", "unrelated")],
                                 value="new", allow_blank=False, id="a-rel")
                    yield Input(placeholder="lobby refs: L0003 L0007", id="a-refs")
                yield Button("+ add claim", id="a-add", variant="primary")
                yield Static("", id="a-claims")
                yield Label("caused (what this retrieval did)")
                yield Input(placeholder="e.g. answers L0003 / opened new bounty / nothing: off-topic", id="a-caused")
                yield Input(placeholder="caused refs: L0003", id="a-caused-refs")
                with Horizontal(classes="row"):
                    yield Button("submit", id="a-submit", variant="success")
                    yield Button("nothing useful", id="a-nothing")
                    yield Button("cancel", id="a-cancel", variant="error")
                yield Static("", id="a-err")

    def _err(self, msg: str):
        self.query_one("#a-err", Static).update(Text(msg, self.st["error"]))

    def _render_claims(self):
        t = Text()
        for i, c in enumerate(self.claims, 1):
            t.append(f"{i}. ", self.st["number"])
            t.append(f"[{c.relation}] ", self.st["type"])
            t.append(c.text, self.st["body"])
            t.append(f"  @{c.pointer.start}:{c.pointer.end}", self.st["meta_ref"])
            if c.lobby_refs:
                t.append("  → " + " ".join(c.lobby_refs), self.st["id"])
            t.append("\n")
        self.query_one("#a-claims", Static).update(t)

    @on(Button.Pressed, "#a-add")
    def add_claim(self):
        claim = self.query_one("#a-claim", Input).value.strip()
        quote = self.query_one("#a-quote", Input).value
        rel = self.query_one("#a-rel", Select).value
        refs = self.query_one("#a-refs", Input).value.split()
        if not claim or not quote.strip():
            return self._err("claim and quote are both required")
        ptr = make_pointer(self.text, self.entry.text_hash, quote)
        if ptr is None:
            return self._err("quote not found in the stored data — copy it from the left pane")
        try:
            self.claims.append(Claim(text=claim, pointer=ptr, relation=rel, lobby_refs=refs))
        except Exception as ex:  # pydantic
            return self._err(str(ex))
        for i in ("#a-claim", "#a-quote", "#a-refs"):
            self.query_one(i, Input).value = ""
        self._err("")
        self._render_claims()

    def _submit(self, caused: str, caused_refs: list[str]):
        try:
            ann = Annotation(target=self.rid, claims=self.claims, caused=caused, caused_refs=caused_refs)
            row = self.hotel.gateway.annotate(ann, actor="user")
        except AnnotationRejected as ex:
            return self._err("rejected:\n" + "\n".join(ex.errors))
        except Exception as ex:
            return self._err(str(ex))
        self.dismiss(row.id)

    @on(Button.Pressed, "#a-submit")
    def submit(self):
        caused = self.query_one("#a-caused", Input).value.strip()
        if not caused:
            return self._err("say what the retrieval caused")
        self._submit(caused, self.query_one("#a-caused-refs", Input).value.split())

    @on(Button.Pressed, "#a-nothing")
    def nothing(self):
        self.claims = []
        self._submit("nothing: retrieval not useful", [])

    @on(Button.Pressed, "#a-cancel")
    def action_cancel(self):
        self.dismiss(None)


# ======================================================================== app
class HotelApp(App):
    TITLE = "hotel"
    CSS = """
    Screen { background: $background; color: $foreground; }
    #status { height: 1; padding: 0 1; background: $panel; color: $hotel-muted; }
    #main { height: 1fr; }
    #dir { width: 34; border: round $border-blurred; background: $background; padding: 0 1; }
    #dir:focus { border: round $border; }
    #lobby-wrap { width: 1fr; border: round $border-blurred; background: $background;
                  border-title-color: $hotel-title; border-title-style: bold; }
    #lobby-wrap:focus, #lobby-wrap:focus-within { border: round $border; }
    #side { width: 72; }
    #side TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    #reg-table { height: 1fr; background: $background; }
    .pane-scroll { height: 1fr; background: $background; padding: 0 1; }
    #msg { height: auto; max-height: 8; padding: 0 1; color: $hotel-muted; background: $background; }
    #cmd { border: round $border-blurred; background: $background; }
    #cmd:focus { border: round $border; }
    Input { background: $hotel-element; border: tall $hotel-element; }
    Input:focus { border: tall $border; }
    Input > .input--placeholder { color: $hotel-placeholder; }
    Tree { background: $background; }
    DataTable > .datatable--header { background: $background; color: $hotel-muted; text-style: bold; }
    DataTable > .datatable--cursor { background: $block-cursor-background; }
    Tabs { background: $background; }
    Underline > .underline--bar { color: $primary; background: $border-blurred; }

    AnnotateScreen { align: center middle; background: $background 70%; }
    #ann { width: 96%; height: 92%; }
    #ann-blob { width: 1fr; border: round $border-blurred; padding: 0 1; background: $background;
                border-title-color: $hotel-title; }
    #ann-form { width: 62; border: round $border; padding: 0 1; background: $background;
                border-title-color: $hotel-title; }
    #ann-form Label { color: $hotel-muted; margin-top: 1; }
    #ann-form .row { height: auto; }
    #ann-form Select { width: 18; }
    #ann-form #a-refs { width: 1fr; }
    #ann-form Button { margin: 1 1 0 0; min-width: 10; }
    #a-claims { margin-top: 1; height: auto; }
    #a-err { margin-top: 1; height: auto; }
    """

    BINDINGS = [
        Binding("ctrl+a", "annotate", "annotate", priority=True),
        Binding("f2", "annotate", "annotate", show=False),
        Binding("ctrl+l", "librarian", "librarian", priority=True),
        Binding("ctrl+t", "toggle_theme", "theme", priority=True),
        Binding("ctrl+r", "refresh", "refresh", priority=True),
        Binding("ctrl+q", "quit", "quit", priority=True),
        Binding("escape", "focus_cmd", "command", show=False),
    ]

    def __init__(self, hotel: Hotel):
        super().__init__()
        self.hotel = hotel
        self.pals = palettes()
        self.pal_name = hotel.cfg.get("theme", "calculus-dark")
        self.room = hotel.cfg.get("default_room", "inbox")
        self.focus_id: str | None = None
        self.guest_mode = False
        self.guest = None
        self.guest_log = Text()
        self.agent_lock = threading.Lock()
        self.positions: dict[str, int] = {}
        for p in self.pals.values():  # must exist before the stylesheet is parsed
            self.register_theme(textual_theme(p))
        self.theme = self.pal_name

    @property
    def st(self):
        return lobby_styles(self.pals[self.pal_name])

    # ------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="main"):
            yield Tree("lobby", id="dir")
            with VerticalScroll(id="lobby-wrap"):
                yield Static(id="lobby")
            with Vertical(id="side"):
                with TabbedContent(initial="t-reg"):
                    with TabPane("Reg", id="t-reg"):
                        yield DataTable(id="reg-table", cursor_type="row", zebra_stripes=False)
                    with TabPane("Results", id="t-res"):
                        with VerticalScroll(classes="pane-scroll"):
                            yield Static(id="results")
                    with TabPane("Review", id="t-rev"):
                        with VerticalScroll(classes="pane-scroll"):
                            yield Static(id="review")
                    with TabPane("Guest", id="t-guest"):
                        with VerticalScroll(classes="pane-scroll", id="guest-scroll"):
                            yield Static(id="guest")
                    with TabPane("Blob", id="t-blob"):
                        with VerticalScroll(classes="pane-scroll"):
                            yield Static(id="blob")
        yield Static(id="msg")
        yield Input(placeholder="write a thought, or /help", id="cmd")
        yield Footer()

    def on_mount(self):
        self.query_one("#dir").border_title = "directory"
        self.query_one("#lobby-wrap").border_title = "lobby.txt"
        self.query_one("#cmd").border_title = "write"
        t = self.query_one("#reg-table", DataTable)
        t.add_columns("id", "time", "kind", "actor", "tool", "status", "")
        errs = self.hotel.sync_hand_edits()
        if errs:
            self.say("lobby.txt has invalid hand edits vs HEAD — fix before writing:\n" + "\n".join(errs[:6]), "error")
        else:
            self.say("welcome. /help for commands. plain text is a monologue in [b]" + self.room + "[/b].")
        self.refresh_all()
        self.query_one("#cmd", Input).focus()

    # ------------------------------------------------------------- render
    def say(self, msg: str | Text, style: str = "muted"):
        w = self.query_one("#msg", Static)
        if isinstance(msg, Text):
            w.update(msg)
        else:
            w.update(Text.from_markup(msg, style=self.st.get(style, self.st["muted"])))

    def refresh_all(self):
        self.render_status()
        self.render_lobby()
        self.render_dir()
        self.render_reg()
        self.render_review()

    def render_status(self):
        st = self.st
        pend = self.hotel.gateway.pending()
        t = Text()
        t.append(" HOTEL ", st["room_name"])
        t.append("· session ", st["muted"])
        t.append(self.hotel.session_id[-12:], st["number"])
        t.append(" · room ", st["muted"])
        t.append(self.room, st["type"])
        t.append(" · gate ", st["muted"])
        if pend:
            t.append("LOCKED ", st["deleted"])
            t.append(" ".join(p.rid for p in pend), st["meta_ref"])
            t.append(" (ctrl+a)", st["muted"])
        else:
            t.append("open", st["created"])
        t.append(" · librarian ", st["muted"])
        t.append(self.hotel.cfg.get("librarian", "rule"), st["function"])
        if self.guest_mode:
            t.append(" · guest ", st["muted"])
            t.append(self.hotel.cfg.get("llm", "mercury"), st["hint"])
        n = len(self.hotel.reg.pending_reviews())
        if n:
            t.append(" · review ", st["muted"])
            t.append(str(n), st["modified"])
        self.query_one("#status", Static).update(t)

    def render_lobby(self):
        text, self.positions = highlight_lobby(self.hotel.text(), self.st, self.focus_id)
        self.query_one("#lobby", Static).update(text)

    def render_dir(self):
        tree = self.query_one("#dir", Tree)
        tree.clear()
        st = self.st
        try:
            lob = self.hotel.lobby()
        except Exception as ex:
            tree.root.add_leaf(Text(str(ex)[:60], st["error"]))
            return
        tree.root.set_label(Text(f"lobby · {len(lob.entries)}", st["room_name"]))
        for room in lob.rooms:
            ents = lob.in_room(room)
            live = [e for e in ents if e.sigil in (" ", "!")]
            lbl = Text()
            lbl.append(room, st["type"] if room == self.room else st["body"])
            lbl.append(f" {len(live)}/{len(ents)}", st["muted"])
            node = tree.root.add(lbl, data=("room", room), expand=True)
            for e in live:
                l2 = Text()
                l2.append(f"[{e.sigil}] ", st["sig_open"] if e.sigil == " " else st["sig_contested"])
                l2.append(e.id + " ", st["id"])
                l2.append(e.type[:4] + " ", st["muted"])
                l2.append(e.body[0][:40], st["body"])
                node.add_leaf(l2, data=("entry", e.id))
        tree.root.expand()

    def render_reg(self):
        t = self.query_one("#reg-table", DataTable)
        t.clear()
        st = self.st
        kind_style = {"retrieval": "function", "annotation": "created", "lobby_edit": "modified",
                      "model": "hint", "blocked": "deleted", "review": "sig_contested",
                      "decision": "success", "internal": "muted", "output": "accent", "redaction": "deleted"}
        for e in self.hotel.reg.list(limit=200)[::-1]:
            extra = ""
            if e.kind == "retrieval" and e.status == "ok":
                extra = "annotated" if self.hotel.reg.annotation_for(e.id) else "⚠ needs annotation"
            elif e.kind in ("annotation", "decision", "review") and e.ref_id:
                extra = f"→ R:{e.ref_id}"
            elif e.kind == "lobby_edit" and e.payload:
                extra = ", ".join(o["op"] for o in e.payload.get("ops", [])) or "hand edit"
            elif e.error:
                extra = e.error[:40]
            t.add_row(
                Text(e.rid, st["number"]), Text(e.ts[11:19], st["ts"]),
                Text(e.kind, st[kind_style.get(e.kind, "body")]), Text(e.actor, st["body"]),
                Text(e.tool or "", st["meta_key"]),
                Text(e.status, st["created"] if e.status == "ok" else st["deleted"]),
                Text(extra, st["warning"] if "⚠" in extra else st["muted"]),
                key=str(e.id),
            )

    def render_review(self):
        st = self.st
        t = Text()
        items = self.hotel.reg.pending_reviews()
        if not items:
            t.append("review queue empty", st["muted"])
        for r in items:
            t.append(f"{r.rid} ", st["number"])
            t.append(f"by {r.actor} ", st["muted"])
            t.append(f"(cause R:{r.ref_id})\n" if r.ref_id else "\n", st["muted"])
            t.append(json.dumps(r.payload["op"], ensure_ascii=False, indent=1) + "\n", st["string"])
            t.append(f"reason: {r.payload['reason']}\n", st["meta_note"])
            t.append(f"/approve {r.rid}   /reject {r.rid}\n\n", st["accent"])
        self.query_one("#review", Static).update(t)

    def render_results(self, hits, query, rid):
        st = self.st
        t = Text()
        t.append(f"{rid}  ", st["number"])
        t.append(query + "\n\n", st["room_name"])
        for n, h in enumerate(hits, 1):
            t.append(f"[{n}] ", st["number"])
            t.append(f"{h.source}", st["function"])
            flag_st = {"external": "warning", "own": "created", "secondhand": "hint"}[h.flag]
            t.append(f" {h.flag} ", st[flag_st])
            t.append((h.title or h.id) + "\n", st["body"])
            if h.url:
                t.append(f"    {h.url}\n", st["meta_ref"])
            t.append(f"    {h.snippet}\n\n", st["muted"])
        if not hits:
            t.append("no results\n", st["muted"])
        self.query_one("#results", Static).update(t)
        self.query_one(TabbedContent).active = "t-res"

    def show_blob(self, rid: int):
        e = self.hotel.reg.get(rid)
        if not e or not e.text_hash:
            return self.say(f"R:{rid} has no text blob", "error")
        try:
            body = Text(self.hotel.blobs.text(e.text_hash), self.st["body"])
        except FileNotFoundError:
            body = Text(f"R:{rid}: content forgotten (see the redaction row in the Reg)", self.st["muted"])
        self.query_one("#blob", Static).update(body)
        self.query_one(TabbedContent).active = "t-blob"

    # ------------------------------------------------------------- events
    @on(Tree.NodeSelected, "#dir")
    def node_selected(self, ev: Tree.NodeSelected):
        if not ev.node.data:
            return
        kind, val = ev.node.data
        if kind == "room":
            self.room = val
            self.render_status()
        else:
            self.focus_id = val
            self.render_lobby()
            if val in self.positions:
                self.query_one("#lobby-wrap", VerticalScroll).scroll_to(y=max(0, self.positions[val] - 3), animate=False)

    @on(DataTable.RowSelected, "#reg-table")
    def reg_selected(self, ev: DataTable.RowSelected):
        self.show_blob(int(ev.row_key.value))

    @on(Input.Submitted, "#cmd")
    def submitted(self, ev: Input.Submitted):
        line = ev.value.strip()
        ev.input.value = ""
        if not line:
            return
        try:
            self.dispatch(line)
        except GateBlocked as ex:
            self.say(f"⛔ {ex}", "error")
        except ValidationFailed as ex:
            self.say("validator rejected:\n" + "\n".join(ex.errors[:6]), "error")
        except (OpError, ValueError) as ex:
            self.say(str(ex), "error")
        self.refresh_all()

    def action_focus_cmd(self):
        self.query_one("#cmd", Input).focus()

    def action_refresh(self):
        self.refresh_all()

    def action_toggle_theme(self):
        self.pal_name = "calculus-light" if self.pal_name == "calculus-dark" else "calculus-dark"
        self.theme = self.pal_name
        self.refresh_all()

    def action_annotate(self, rid: int | None = None):
        pend = self.hotel.gateway.pending()
        if rid is None:
            if not pend:
                return self.say("nothing to annotate — gate is open")
            rid = pend[0].id

        def done(result):
            if result is None:
                return
            self.say(f"annotation R:{result} recorded for R:{rid}", "success")
            if self.hotel.cfg.get("auto_librarian", True):
                self.librarian_on(rid)
            self.refresh_all()

        self.push_screen(AnnotateScreen(self.hotel, rid, self.st), done)

    def action_librarian(self):
        self.librarian_on(None)

    # ------------------------------------------------------------- commands
    def dispatch(self, line: str):
        h = self.hotel
        if not line.startswith("/"):
            if self.guest_mode and line.startswith(">"):
                # conversational turn ("yes, draft it") — talk to the guest, don't write it down
                if h.gateway.pending():
                    raise GateBlocked(h.gateway.pending())
                self.say("guest thinking…")
                return self.run_guest(line[1:].strip(), None)
            res = h.apply([AppendOp(op="append", room=self.room, type="monologue", body=line, refs=[], tags=[])])
            self.focus_id = res.created[0]
            if self.guest_mode:
                if h.gateway.pending():
                    raise GateBlocked(h.gateway.pending())
                self.say(f"+ {res.created[0]} monologue · guest thinking…", "created")
                return self.run_guest(line, res.created[0])
            return self.say(f"+ {res.created[0]} monologue in {self.room}", "created")
        cmd, _, rest = line[1:].partition(" ")
        rest = rest.strip()
        if cmd == "guest":
            if rest == "new" or self.guest is None:
                self.guest = None  # created lazily in the worker (needs the model profile)
                self.guest_log = Text()
            self.guest_mode = rest != "off"
            self.render_status()
            return self.say("guest mode " + ("on: your lines become monologue entries; the guest replies in "
                                             "the Guest tab and drafts go to Review" if self.guest_mode else "off"))
        if cmd == "talk":
            if not rest:
                raise ValueError("/talk <message>  (talk to the guest without writing a note)")
            if h.gateway.pending():
                raise GateBlocked(h.gateway.pending())
            return self.run_guest(rest, None)
        if cmd == "move":
            parts = rest.split(maxsplit=2)
            if len(parts) < 2:
                raise ValueError("/move L#### <room> [reason]")
            res = h.apply([MoveOp(op="move", id=parts[0], room=parts[1].lower(),
                                  reason=parts[2] if len(parts) > 2 else "refiled")])
            self.focus_id = res.created[0]
            return self.say(f"{parts[0]} moved to {parts[1]} as {res.created[0]}", "modified")
        if cmd == "custodian":
            results = h.custodian()
            return self.say(f"custodian: {len(results)} ops · " + ", ".join(f"{r.op.op} {r.route}" for r in results)
                            if results else "custodian: nothing stale")
        if cmd == "backup":
            from .backup import backup
            info = backup(h.paths, Path(rest or h.cfg.get("backup_dir") or "~/hotel-backups"))
            return self.say(f"backup → {info['snapshot']} ({info['reg_rows']} reg rows, "
                            f"{info['blobs_copied']} new blobs)", "success")
        args = shlex.split(rest, posix=True) if rest and cmd not in TYPE_CMDS and cmd not in (
            "s", "exa", "ls", "supersede", "contest", "reopen") else rest.split()

        if cmd in ("q", "quit"):
            return self.exit()
        if cmd == "help":
            return self.say(HELP, "body")
        if cmd in TYPE_CMDS:
            if not rest:
                raise ValueError(f"/{cmd} needs text")
            res = h.apply([AppendOp(op="append", room=self.room, type=TYPE_CMDS[cmd], body=rest, refs=[], tags=[])])
            self.focus_id = res.created[0]
            return self.say(f"+ {res.created[0]} {TYPE_CMDS[cmd]} in {self.room}", "created")
        if cmd == "room":
            if not args:
                raise ValueError("/room <slug>")
            self.room = args[0].lower()
            return self.say(f"room → {self.room} (created on first entry)")
        if cmd in ("s", "exa", "fetch"):
            if not rest:
                raise ValueError(f"/{cmd} needs a query")
            tool = {"s": "search", "exa": "exa.search", "fetch": "exa.fetch"}[cmd]
            # gate check up front so the UI refuses instantly
            if h.gateway.pending():
                raise GateBlocked(h.gateway.pending())
            self.say(f"… {tool}: {rest}")
            return self.run_search(tool, rest)
        if cmd == "ls":
            e, res = h.gateway.call("lobby.search", actor="user", query=rest)
            return self.render_results(res.data, rest, e.rid)
        if cmd == "annotate":
            rid = int(args[0].removeprefix("R:")) if args else None
            return self.action_annotate(rid)
        if cmd == "close":
            if len(args) != 2:
                raise ValueError("/close L#### R:n")
            h.apply([CloseOp(op="close", id=args[0], closed_by=args[1])])
            return self.say(f"closed {args[0]} by {args[1]}", "created")
        if cmd == "supersede":
            eid, _, body = rest.partition(" ")
            e = h.lobby().entries.get(eid)
            if not e or not body:
                raise ValueError("/supersede L#### <new text>")
            res = h.apply([SupersedeOp(op="supersede", id=eid, type=e.type, body=body, reason="user correction")])
            self.focus_id = res.created[0]
            return self.say(f"{eid} superseded by {res.created[0]}", "modified")
        if cmd in ("contest", "reopen"):
            eid, _, why = rest.partition(" ")
            Op = ContestOp if cmd == "contest" else ReopenOp
            h.apply([Op(op=cmd, id=eid, reason=why or "user")])
            return self.say(f"{cmd} {eid}", "modified")
        if cmd in ("tag", "link", "ref"):
            if len(args) != 2:
                raise ValueError(f"/{cmd} L#### <value>")
            op = {"tag": lambda: TagOp(op="tag", id=args[0], tag=args[1]),
                  "link": lambda: LinkOp(op="link", id=args[0], target=args[1]),
                  "ref": lambda: RefOp(op="ref", id=args[0], ref=args[1])}[cmd]()
            h.apply([op])
            return self.say(f"{cmd} {args[0]} {args[1]}", "created")
        if cmd == "promote":
            e = h.lobby().entries.get(args[0] if args else "")
            if not e:
                raise ValueError("/promote L####")
            h.apply([PromoteOp(op="promote", id=e.id, text=e.text)])
            return self.say(f"promoted {e.id} into RAG (logged, backlinked)", "created")
        if cmd == "research":
            if not args or args[0] not in h.lobby().entries:
                raise ValueError("/research L####  (an existing entry, usually a bounty)")
            if h.gateway.pending():
                raise GateBlocked(h.gateway.pending())
            self.say(f"… research agent on {args[0]} ({h.cfg.get('llm', 'mercury')})")
            return self.run_research(args[0])
        if cmd == "accept":
            res = h.accept_output(int(args[0].removeprefix("R:")))
            self.focus_id = res.created[0]
            return self.say(f"+ {res.created[0]} reasoning (origin=approved) from {args[0]}", "created")
        if cmd == "lib":
            return self.librarian_on(int(args[0].removeprefix("R:")) if args else None)
        if cmd in ("approve", "reject"):
            r = h.decide(int(args[0].removeprefix("R:")), cmd == "approve")
            return self.say(f"{cmd}: {r.route} — {r.reason} {r.detail}", "created" if r.route == "applied" else "warning")
        if cmd == "blob":
            return self.show_blob(int(args[0].removeprefix("R:")))
        if cmd == "check":
            errs = h.check()
            return self.say("lobby valid ✓" if not errs else "\n".join(errs[:8]), "success" if not errs else "error")
        if cmd == "verify":
            p = h.reg.verify_chain()
            return self.say("reg hash chain intact ✓" if not p else "\n".join(p[:8]), "success" if not p else "error")
        if cmd == "theme":
            return self.action_toggle_theme()
        raise ValueError(f"unknown command /{cmd} — /help")

    @work(thread=True, exclusive=True)
    def run_search(self, tool: str, query: str):
        h = self.hotel
        try:
            kw = {"url": query} if tool == "exa.fetch" else {"query": query}
            entry, res = h.gateway.call(tool, actor="user", **kw)
        except GateBlocked as ex:
            self.call_from_thread(self.say, f"⛔ {ex}", "error")
            return
        except Exception as ex:
            self.call_from_thread(self.say, f"{tool} failed (logged): {ex}", "error")
            self.call_from_thread(self.refresh_all)
            return

        def ui():
            self.render_results(res.data, query, entry.rid)
            self.say(f"{entry.rid} stored ({len(res.data)} hits, text {entry.text_hash[:10]}). "
                     f"gate locked — ctrl+a to annotate.", "warning")
            self.refresh_all()
        self.call_from_thread(ui)

    @work(thread=True, group="agent")
    def run_guest(self, text: str, note_id: str | None):
        with self.agent_lock:  # turns run in order; a fast second message waits, never races
            self._run_guest(text, note_id)

    def _run_guest(self, text: str, note_id: str | None):
        from .guest import Guest
        h = self.hotel
        st = self.st
        try:
            if self.guest is None:
                self.guest = Guest(h, on_event=lambda s: self.call_from_thread(self.say, f"guest · {s}"))
            turn = self.guest.say(text, note_id)
        except Exception as ex:
            self.call_from_thread(self.say, f"guest failed (logged): {ex}", "error")
            self.call_from_thread(self.refresh_all)
            return
        log = self.guest_log
        log.append("you" + (f" ({note_id})" if note_id else "") + "\n", st["muted"])
        log.append(text + "\n", st["body"])
        log.append(f"guest R:{turn.output_id}\n" if turn.output_id else "guest (no reply)\n", st["hint"])
        log.append((turn.reply or "; ".join(turn.log[-2:])) + "\n", st["string"] if turn.reply else st["error"])
        if turn.proposals:
            log.append("drafts: " + " ".join(f"R:{p}" for p in turn.proposals) + "  (/approve R:n)\n", st["modified"])
        log.append("\n")

        def ui():
            self.query_one("#guest", Static).update(log)
            self.query_one(TabbedContent).active = "t-guest"
            self.query_one("#guest-scroll", VerticalScroll).scroll_end(animate=False)
            self.say(f"guest replied{' · ' + str(len(turn.proposals)) + ' draft(s) in Review' if turn.proposals else ''}",
                     "hint")
            self.refresh_all()
        self.call_from_thread(ui)

    @work(thread=True, group="agent")
    def run_research(self, entry_id: str):
        with self.agent_lock:
            self._run_research(entry_id)

    def _run_research(self, entry_id: str):
        from .agent import ResearchAgent
        h = self.hotel
        agent = ResearchAgent(h, max_searches=h.cfg.get("agent_max_searches", 4),
                              budget_chars=h.cfg.get("agent_budget_chars", 40000),
                              on_event=lambda s: self.call_from_thread(self.say, f"agent · {s}"))
        try:
            run = agent.run(entry_id)
            for rid in run.retrievals:
                if h.reg.annotation_for(rid):
                    run_librarian(h, rid)
        except Exception as ex:
            self.call_from_thread(self.say, f"research failed (logged): {ex}", "error")
            self.call_from_thread(self.refresh_all)
            return
        st = self.st
        t = Text()
        if run.output_id:
            t.append(f"R:{run.output_id} ", st["number"])
            t.append(run.answer + chr(10), st["body"])
            t.append(f"{run.timings()} · /accept R:{run.output_id} to keep it", st["muted"])
        else:
            t.append("no answer: " + "; ".join(run.log[-2:]), st["error"])

        def ui():
            self.say(t)
            if run.output_id:
                self.show_blob_text(f"R:{run.output_id} research answer for {entry_id}", run.answer)
            self.refresh_all()
        self.call_from_thread(ui)

    def show_blob_text(self, title: str, text: str):
        self.query_one("#blob", Static).update(Text(title + chr(10) * 2, self.st["room_name"]) + Text(text, self.st["body"]))
        self.query_one(TabbedContent).active = "t-blob"

    @work(thread=True, exclusive=True, group="lib")
    def librarian_on(self, rid: int | None):
        h = self.hotel
        if rid is None:
            anns = h.reg.list(kind="annotation")
            anns = [a for a in anns if a.status == "ok"]
            if not anns:
                self.call_from_thread(self.say, "no annotated retrieval yet", "error")
                return
            rid = anns[-1].ref_id
        try:
            out, results = run_librarian(h, rid)
        except Exception as ex:
            self.call_from_thread(self.say, f"librarian failed (logged if a model call): {ex}", "error")
            self.call_from_thread(self.refresh_all)
            return
        st = self.st
        t = Text()
        t.append(f"librarian on R:{rid}: {len(results)} ops · {out.rationale[:70]}\n", st["function"])
        for r in results:
            sty = {"applied": "created", "queued": "modified", "rejected": "deleted", "noop": "muted"}[r.route]
            t.append(f"  {r.route:<8}", st[sty])
            t.append(f" {r.op.op} {getattr(r.op, 'id', getattr(r.op, 'room', ''))} ", st["body"])
            t.append(f"{r.reason} {r.detail}\n", st["muted"])

        def ui():
            self.say(t)
            self.refresh_all()
        self.call_from_thread(ui)
