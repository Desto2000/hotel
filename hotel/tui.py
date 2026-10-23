"""hotel TUI — the lobby is the product; the TUI is a cockpit over it.

Typing plain text appends a monologue entry to the current room. Everything
else is a /command. External retrievals lock the annotation gate until you
annotate them (ctrl+a).
"""

from __future__ import annotations

import json
import random
import re
import shlex
import threading
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, DataTable, Footer, Input, Label, Select, Static, TabbedContent, TabPane, Tree,
)

from . import keys, reading
from .core import Hotel, ValidationFailed
from .gateway import AnnotationRejected, GateBlocked, load_annotation, make_pointer
from .grammar import SIGIL_NAMES
from .librarian import run_librarian
from .models import (
    Annotation, AppendOp, Claim, CloseOp, ContestOp, LinkOp, MoveOp, PromoteOp, RefOp, ReopenOp, SupersedeOp, TagOp,
)
from .ops import OpError
from .render import BUILDING, WINDOWS, building, highlight_lobby
from .theme import lobby_styles, palettes, textual_theme

TYPE_CMDS = {"bounty": "bounty", "goal": "goal", "idea": "idea", "reason": "reasoning",
             "log": "log", "mono": "monologue"}

HELP = r"""[b]write[/b]   plain text → monologue in current room · /bounty /goal /idea /reason /log <text>
[b]rooms[/b]   /room <slug>
[b]search[/b]  /s <q> unified (exa+rag+lobby) · /exa <q> · /deep <q> (Exa Deep Search) · /fetch <url> · /ls <q> lobby only (ungated)
[b]gate[/b]    /annotate \[R:n] (ctrl+a) — required after every external retrieval
[b]edit[/b]    /close L R:n · /supersede L <text> · /contest L <why> · /reopen L <why>
        /tag L t · /link L L · /ref L R:n · /promote L
[b]guest[/b]   /guest (on) · /guest new · /guest off — your lines become notes, the guest replies, drafts → Review
        in guest mode, start a line with > to talk without writing it down (e.g. "> yes, draft it") · /talk <msg>
[b]agent[/b]   /research L#### (one bounty per run) · /accept R:n keep an answer as an approved entry
[b]house[/b]   /move L <room> \[why] · /custodian (flag stale closed bounties) · /backup \[dir]
[b]resident[/b] /lib \[R:n] run librarian · /approve R:n · /reject R:n
[b]inspect[/b] /blob R:n\[#ck] \[all|raw] (a claim: just its section, quote highlighted) · /check · /verify · /theme (ctrl+t) · /setup (keys, privacy, theme) · /q"""


# ======================================================================== opening
class Building(Static):
    """The hotel: the street draws out, it rises floor by floor, the windows light up,
    the sign flickers on. Colours follow the app's current theme."""

    def __init__(self, on_done=None, **kw):
        super().__init__(**kw)
        self.on_done = on_done
        wins = list(WINDOWS)
        random.shuffle(wins)
        street = len(BUILDING) - 1
        half = (len(BUILDING[-1]) + 1) // 2
        self.frames = [(street, k, set(), False) for k in range(1, half + 1, 2)]  # street draws out
        self.frames += [(street - n, half, set(), False) for n in range(1, street + 1)]  # floors rise
        for i in range(0, len(wins) + 1, 2):  # windows light up
            self.frames.append((0, half, set(wins[:i]), False))
        lit = set(wins[:-4])  # a few stay dark, like a real hotel at night
        for on in (True, False, True, False, False, True, True, True, True, True, True):  # neon flicker, hold
            self.frames.append((0, half, lit, on))
        self.i = 0

    def on_mount(self):
        self.repaint()
        self.timer = self.set_interval(0.06, self._step)

    def _step(self):
        if self.i + 1 >= len(self.frames):
            self.timer.stop()
            if self.on_done:
                self.on_done()
            return
        self.i += 1
        self.repaint()

    def finish(self):
        """Jump to the last frame (lit hotel, sign on)."""
        self.timer.stop()
        self.i = len(self.frames) - 1
        self.repaint()

    def repaint(self):
        self.update(self._frame(*self.frames[self.i]))

    def _frame(self, top: int, street_k: int, lit: set, sign: bool) -> Text:
        return building(self.app.st, top, street_k, lit, sign)


class OpeningScreen(Screen):
    """Startup animation. Ends by itself; any key skips (and is typed into the prompt)."""

    CSS = "OpeningScreen { align: center middle; background: $background; } #opening { width: auto; }"

    def compose(self) -> ComposeResult:
        yield Building(on_done=lambda: self.dismiss(None), id="opening")

    def on_key(self, event):
        event.stop()
        self.query_one(Building).timer.stop()
        self.dismiss(event.character if event.is_printable else None)

    def on_click(self):
        self.query_one(Building).timer.stop()
        self.dismiss(None)


class OnboardingScreen(Screen):
    """First-run setup, one step at a time: welcome, each API key (checked live against
    the provider), the Mercury training opt-out, the theme (previewed live), ready.
    Enter moves on; esc finishes later (`/setup` reopens it)."""

    BINDINGS = [Binding("escape", "finish", "finish later", priority=True)]
    CSS = """
    OnboardingScreen { align: center middle; background: $background; }
    #ob { width: 76; height: auto; }
    #ob-art { width: auto; margin-bottom: 1; }
    #ob-title, #ob-sub, #ob-steps { width: 100%; text-align: center; }
    #ob-title { text-style: bold; color: $hotel-title; }
    #ob-sub { color: $hotel-muted; }
    #ob-steps { margin: 1 0; }
    #ob-card { width: 100%; height: auto; min-height: 9; border: round $border; padding: 0 2;
               border-title-color: $hotel-title; border-title-style: bold; }
    #ob-body { height: auto; }
    #ob-key { margin-top: 1; }
    #ob-note { height: auto; }
    #ob-hint { width: 100%; text-align: center; color: $hotel-muted; margin-top: 1; }
    """

    def __init__(self, hotel: Hotel, *, check: bool = True, probe=None):
        super().__init__()
        self.hotel, self.check, self.probe = hotel, check, probe
        self.steps: list = ["welcome", *keys.PROVIDERS]
        if not hotel.cfg.get("inception_training_opt_out_confirmed"):
            self.steps.append("privacy")
        self.steps += ["theme", "ready"]
        self.n = 0
        self.note = Text()
        self.busy = False

    @property
    def step(self):
        return self.steps[self.n]

    def compose(self) -> ComposeResult:
        with Vertical(id="ob"):
            with Center():
                yield Building(id="ob-art")
            yield Static("Welcome to hotel", id="ob-title")
            yield Static("a logged, living lobby for what you learn", id="ob-sub")
            yield Static(id="ob-steps")
            with Vertical(id="ob-card"):
                yield Static(id="ob-body")
                yield Input(password=True, id="ob-key")
                yield Static(id="ob-note")
            yield Static(id="ob-hint")

    def on_mount(self):
        self.show()

    # ------------------------------------------------------------- render
    def show(self):
        st, step = self.app.st, self.step
        self.query_one("#ob-steps", Static).update(self._progress())
        card = self.query_one("#ob-card")
        body, hint = Text(), ""
        inp = self.query_one("#ob-key", Input)
        inp.display = False
        if step == "welcome":
            card.border_title = "welcome"
            body.append("hotel replaces “chat with AI” as note-taking. Everything you and the\n", st["body"])
            body.append("models do goes through a logged Reg; what lasts is one plain-text\n", st["body"])
            body.append("document, the lobby.\n\n", st["body"])
            body.append("Setup takes a minute: ", st["muted"])
            body.append("API keys", st["type"])
            body.append(", ", st["muted"])
            body.append("privacy", st["type"])
            body.append(", ", st["muted"])
            body.append("theme", st["type"])
            body.append(".", st["muted"])
            hint = "enter  get started   ·   esc  later"
        elif isinstance(step, keys.Provider):
            p, src = step, keys.source(step.env)
            card.border_title = p.name
            body.append("required" if p.required else "optional", st["warning"] if p.required else st["muted"])
            body.append(f" · {p.purpose}\n\n", st["body"])
            if src == "env":
                body.append("✓ ", st["success"])
                body.append(f"{p.env} is set in your environment", st["body"])
                hint = "enter  continue   ·   esc  later"
            else:
                if src == "file":
                    body.append("✓ ", st["success"])
                    body.append(f"stored key {keys.mask(keys.get(p.env) or '')}", st["body"])
                    body.append(" · paste a new one to replace it", st["muted"])
                else:
                    body.append("get a key  ", st["muted"])
                    body.append(p.url, st["meta_ref"])
                inp.display = True
                inp.placeholder = f"paste {p.env}"
                inp.disabled = False
                inp.value = ""
                inp.focus()
                hint = "enter  save   ·   enter on empty  " + ("keep" if src == "file" else "skip") + "   ·   esc  later"
        elif step == "privacy":
            card.border_title = "privacy"
            body.append("Mercury trains on API traffic unless you opt out in the Inception\n", st["body"])
            body.append("dashboard. hotel won't send lobby content to it until you confirm.\n\n", st["body"])
            body.append("Have you turned the training opt-in ", st["body"])
            body.append("off", st["warning"])
            body.append("?\n\n", st["body"])
            body.append("  y ", st["accent"])
            body.append(" yes, it's off       ", st["body"])
            body.append("  n ", st["accent"])
            body.append(" not yet", st["body"])
            hint = "y / n   ·   esc  later"
        elif step == "theme":
            card.border_title = "theme"
            body.append("Pick a look. It changes as you move.\n\n", st["body"])
            for name in self.app.pals:
                cur = name == self.app.pal_name
                body.append(" ❯ " if cur else "   ", st["accent"])
                body.append(name + "\n", st["room_name"] if cur else st["muted"])
            hint = "↑ ↓  choose   ·   enter  confirm"
        else:
            card.border_title = "ready"
            left = keys.missing_required()
            body.append("You're checked in.\n\n" if not left else "Almost there.\n\n", st["room_name"])
            for p in keys.PROVIDERS:
                ok = keys.source(p.env) is not None
                body.append(" ✓ " if ok else " · ", st["success"] if ok else st["dead"])
                body.append(f"{p.name:<22}", st["body"] if ok else st["muted"])
                body.append("ready\n" if ok else ("missing\n" if p.required else "skipped\n"),
                            st["muted"] if ok or not p.required else st["warning"])
            if left:
                body.append("\nsearch and model features stay off until the required keys are set (/setup)",
                            st["muted"])
            hint = "enter  open the lobby"
        self.query_one("#ob-body", Static).update(body)
        self.query_one("#ob-note", Static).update(self.note)
        self.query_one("#ob-hint", Static).update(Text(hint))

    def _progress(self) -> Text:
        st, t = self.app.st, Text()
        n_keys = len(keys.PROVIDERS)
        groups = [("welcome", ["welcome"]), ("keys", [s for s in self.steps if isinstance(s, keys.Provider)])]
        groups += [(s, [s]) for s in ("privacy", "theme", "ready") if s in self.steps]
        for i, (label, members) in enumerate(groups):
            if i:
                t.append("  ─  ", st["dead"])
            if self.step in members:
                if label == "keys":
                    label = f"keys {members.index(self.step) + 1}/{n_keys}"
                t.append("● " + label, st["accent"])
            elif self.steps.index(members[-1]) < self.n:
                t.append("✓ " + label, st["success"])
            else:
                t.append("○ " + label, st["muted"])
        return t

    def _say(self, msg: str = "", style: str = "muted"):
        self.note = Text(msg, self.app.st[style]) if msg else Text()
        self.query_one("#ob-note", Static).update(self.note)

    def advance(self):
        self.query_one(Building).finish()
        self.note = Text()
        if self.step == "ready":
            return self.action_finish()
        self.n += 1
        self.show()

    # ------------------------------------------------------------- input
    @on(Input.Submitted, "#ob-key")
    def submitted(self, ev: Input.Submitted):
        ev.stop()
        if self.busy:
            return
        key = ev.value.strip()
        if not key:
            return self.advance()
        p = self.step
        if not self.check:
            keys.set_key(p.env, key)
            return self.advance()
        self.busy = True
        self.query_one("#ob-key", Input).disabled = True
        self._say(f"checking with {p.name}…")
        self.validate(p, key)

    @work(thread=True, exclusive=True)
    def validate(self, p: keys.Provider, key: str):
        good, detail = keys.validate(p, key, probe=self.probe)
        self.app.call_from_thread(self.validated, p, key, good, detail)

    def validated(self, p: keys.Provider, key: str, good: bool | None, detail: str):
        self.busy = False
        inp = self.query_one("#ob-key", Input)
        inp.disabled = False
        if good is False:
            inp.value = ""
            inp.focus()
            return self._say(f"✗ {detail} — check the key and try again", "error")
        keys.set_key(p.env, key)
        self.advance()
        if good is None:  # saved, but say why it is unverified on the next step
            self._say(f"{p.name}: saved {keys.mask(key)} ({detail})", "warning")

    def on_key(self, event):
        step = self.step
        if isinstance(step, keys.Provider) and self.query_one("#ob-key", Input).display:
            return  # the Input handles typing and enter
        k = event.key
        if step == "privacy" and k in ("y", "n"):
            event.stop()
            if k == "y":
                keys.confirm_optout(self.hotel.paths)
                self.hotel.cfg["inception_training_opt_out_confirmed"] = True
            self.advance()
            if k == "n":
                self._say("not confirmed: model features stay blocked until you do (/setup)", "warning")
        elif step == "theme" and k in ("up", "down", "left", "right", "k", "j"):
            event.stop()
            names = list(self.app.pals)
            d = -1 if k in ("up", "left", "k") else 1
            self.app.pal_name = names[(names.index(self.app.pal_name) + d) % len(names)]
            self.app.theme = self.app.pal_name
            self.query_one(Building).repaint()
            self.show()
        elif k == "enter" and step != "privacy":
            event.stop()
            if step == "theme":
                self._save_theme()
            self.advance()

    def _save_theme(self):
        from .config import save_config
        cfg = self.hotel.paths
        saved = json.loads(cfg.config.read_text(encoding="utf-8")) if cfg.config.exists() else {}
        saved["theme"] = self.app.pal_name
        save_config(cfg, saved)
        self.hotel.cfg["theme"] = self.app.pal_name

    def action_finish(self):
        keys.mark_onboarded()
        self.dismiss(None)

# ======================================================================== modal
class AnnotateScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "cancel"),
                Binding("ctrl+o", "toggle_folds", "show/hide folded", priority=True)]

    def __init__(self, hotel: Hotel, rid: int, styles: dict):
        super().__init__()
        self.hotel, self.rid, self.st = hotel, rid, styles
        self.entry = hotel.reg.get(rid)
        self.text = hotel.blobs.text(self.entry.text_hash)
        self.doc = reading.parse(self.text)
        self.show_all = False
        self.claims: list[Claim] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="ann"):
            with VerticalScroll(id="ann-blob") as vs:
                vs.border_title = f"R:{self.rid} · {self.entry.tool} · {self.entry.provider} · text {self.entry.text_hash[:10]}"
                yield Static(id="ann-text")
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

    def on_mount(self):
        self.refresh_view()

    def refresh_view(self):
        """The reading view of the stored text: chrome and long code folded, quotes of the
        claims added so far highlighted. The text itself is never changed."""
        chrome, by = reading.chrome_for(self.hotel, self.rid, self.doc)
        n = len({k for f in reading.folds(self.doc, chrome) for k in range(f.first, f.last + 1)
                 if self.doc.lines[k].kind == "body"})
        hl = [(c.pointer.start, c.pointer.end, self.st["match"]) for c in self.claims]
        self.query_one("#ann-text", Static).update(
            reading.render(self.doc, self.st, chrome, show_all=self.show_all, highlights=hl))
        self.query_one("#ann-blob").border_subtitle = (
            f"{n} lines folded ({by}) · ctrl+o {'folds them' if self.show_all else 'shows them'}" if n else "")

    def action_toggle_folds(self):
        self.show_all = not self.show_all
        self.refresh_view()

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
        self.refresh_view()

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

    def __init__(self, hotel: Hotel, splash: bool | None = None, onboard: bool | None = None):
        super().__init__()
        self.hotel = hotel
        self.splash = splash  # None: play the opening animation unless headless
        self.onboard = onboard  # None: first-run onboarding unless headless or already done
        self.pals = palettes()
        self.pal_name = hotel.cfg.get("theme", "calculus-dark")
        self.room = hotel.cfg.get("default_room", "inbox")
        self.focus_id: str | None = None
        self.guest_mode = False
        self.guest = None
        self.guest_log = Text()
        self.agent_lock = threading.Lock()
        self.positions: dict[str, int] = {}
        self.last_edit: int | None = None  # Reg id of the lobby_edit a plain line produced
        self.results_rid: int | None = None  # retrieval shown in the Results tab
        self.blob_view: tuple | None = None  # (rid, claim, mode) shown in the Blob tab
        from .suggest import NextEdit
        self.suggester = NextEdit(hotel, lambda: self.room)
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
        yield Input(placeholder="write a thought, or /help", id="cmd", suggester=self.suggester)
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
        if self.hotel.index is not None and not self.is_headless:
            self.hotel.index.warm_in_background()  # load (or first-time download) the embedder off the UI thread
        if (not self.is_headless and not keys.onboarded()) if self.onboard is None else self.onboard:
            self.action_setup()
        elif (not self.is_headless) if self.splash is None else self.splash:
            self.push_screen(OpeningScreen(), self._entered)

    def action_setup(self):
        def done(_):
            left = keys.missing_required()
            self.say("checked in. /help for commands, /setup to change keys or theme." if not left else
                     "missing API keys: " + ", ".join(p.env for p in left) + " — /setup when you have them",
                     "success" if not left else "warning")
            self.refresh_all()
            self._entered(None)

        self.push_screen(OnboardingScreen(self.hotel), done)

    def _entered(self, ch: str | None):
        cmd = self.query_one("#cmd", Input)
        if ch:
            cmd.insert_text_at_cursor(ch)
        cmd.focus()

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
        raw = self.hotel.text()
        notes = {r: n for r in set(re.findall(r"\+ ref: (R:\d+#c\d+)", raw)) if (n := self.hotel.claim_text(r))}
        text, self.positions = highlight_lobby(raw, self.st, self.focus_id, notes=notes)
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
        lob = None
        for r in items:
            t.append(f"{r.rid} ", st["number"])
            t.append(f"by {r.actor} ", st["muted"])
            t.append(f"(cause R:{r.ref_id})\n" if r.ref_id else "\n", st["muted"])
            op = r.payload["op"]
            note = self.hotel.claim_text(op["ref"]) if op.get("op") == "ref" else None
            if note:  # a suggested link: show what would be linked to what
                lob = lob or self.hotel.lobby()
                e = lob.entries.get(op["id"])
                t.append("link ", st["muted"])
                t.append(op["id"], st["id"])
                t.append(f" ({e.type}) {' '.join(e.text.split())[:140]}\n" if e else "\n", st["body"])
                t.append("  to ", st["muted"])
                t.append(op["ref"] + " ", st["meta_ref"])
                t.append(note + "\n", st["string"])
                t.append(f"  /blob {op['ref']} shows the section\n", st["muted"])
            else:
                t.append(json.dumps(op, ensure_ascii=False, indent=1) + "\n", st["string"])
            t.append(f"reason: {r.payload['reason']}\n", st["meta_note"])
            t.append(f"/approve {r.rid}   /reject {r.rid}\n\n", st["accent"])
        self.query_one("#review", Static).update(t)

    def render_results(self, rid: int, switch: bool = True):
        """Each hit's header and the start of its real content (from the stored blob, chrome skipped)."""
        st = self.st
        h = self.hotel
        e = h.reg.get(rid)
        doc = reading.parse(h.blobs.text(e.text_hash))
        chrome, by = reading.chrome_for(h, rid, doc)
        query = next((doc.line_text(ln.n)[7:] for ln in doc.lines if ln.kind == "top"
                      and doc.line_text(ln.n).startswith("QUERY: ")), "")
        t = Text()
        t.append(f"R:{rid}  ", st["number"])
        t.append(query + "\n", st["room_name"])
        t.append(f"page chrome skipped ({by}) · /blob R:{rid} reads it all\n\n", st["muted"])
        t.append(reading.preview(doc, st, chrome))
        self.results_rid = rid
        self.query_one("#results", Static).update(t)
        if switch:
            self.query_one(TabbedContent).active = "t-res"

    def show_blob(self, rid: int, claim: int | None = None, mode: str = "", switch: bool = True):
        """mode "" = reading view, "all" = nothing folded, "raw" = the stored text as is.
        With a claim: only the result its quote is in, the quote highlighted."""
        h, st = self.hotel, self.st
        e = h.reg.get(rid)
        if not e or not e.text_hash:
            return self.say(f"R:{rid} has no text blob", "error")
        try:
            text = h.blobs.text(e.text_hash)
        except FileNotFoundError:
            self.query_one("#blob", Static).update(
                Text(f"R:{rid}: content forgotten (see the redaction row in the Reg)", st["muted"]))
            self.query_one(TabbedContent).active = "t-blob"
            return
        self.blob_view = (rid, claim, mode)
        if mode == "raw":
            body = Text(text, st["body"])
        else:
            doc = reading.parse(text)
            chrome, _ = reading.chrome_for(h, rid, doc)
            ann = load_annotation(h.reg.annotation_for(rid), h.blobs) if e.kind == "retrieval" else None
            claims = ann["claims"] if ann else []
            hl = [(c["pointer"]["start"], c["pointer"]["end"], st["match"]) for c in claims]
            body, only = Text(), None
            if claim is not None:
                if not 1 <= claim <= len(claims):
                    return self.say(f"R:{rid} has no claim c{claim}", "error")
                c = claims[claim - 1]
                only = doc.hit_at(c["pointer"]["start"]) or None
                hl = [(c["pointer"]["start"], c["pointer"]["end"], st["selection"])]
                body.append(f"R:{rid}#c{claim} ", st["meta_ref"])
                body.append(f"[{c['relation']}] ", st["type"])
                body.append(c["text"] + "\n", st["room_name"])
                body.append(f"/blob R:{rid} shows the whole retrieval\n\n", st["muted"])
            body.append(reading.render(doc, st, chrome, show_all=mode == "all", highlights=hl, only_hit=only))
        self.query_one("#blob", Static).update(body)
        if switch:
            self.query_one(TabbedContent).active = "t-blob"

    @work(thread=True, group="reader")
    def refine_view(self, rid: int):
        """Let the model fold the chrome the rule missed, then redraw whatever shows this retrieval."""
        h = self.hotel
        if not reading.needs_model(h, rid):
            return
        try:
            lines = reading.model_chrome(h, rid, reading.model_profile(h.cfg) or None)
        except Exception as ex:
            h.events.emit("error", where="reader", error=f"{type(ex).__name__}: {ex}"[:300])
            self.call_from_thread(self.say, f"reading view: model filter failed, rule only ({ex})"[:200], "warning")
            return

        def ui():
            if self.results_rid == rid:
                self.render_results(rid, switch=False)
            if self.blob_view and self.blob_view[0] == rid:
                self.show_blob(*self.blob_view, switch=False)
            for s in self.screen_stack:
                if isinstance(s, AnnotateScreen) and s.rid == rid:
                    s.refresh_view()
            self.render_reg()
            if lines:
                self.say(f"R:{rid}: the model marked {len(lines)} lines as page chrome (folded, not deleted)", "hint")
        self.call_from_thread(ui)

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
        # the command name only: arguments are queries and lobby text, which belong in blobs
        name = "/" + line[1:].partition(" ")[0] if line.startswith("/") else (
            "talk" if self.guest_mode and line.startswith(">") else "monologue")
        events = self.hotel.events
        events.emit("command", name=name, room=self.room)
        self.last_edit = None
        try:
            self.dispatch(line)
        except GateBlocked as ex:
            self.say(f"⛔ {ex}", "error")
        except ValidationFailed as ex:
            events.emit("rejected", name=name, error="; ".join(ex.errors[:3])[:300])
            self.say("validator rejected:\n" + "\n".join(ex.errors[:6]), "error")
        except (OpError, ValueError) as ex:
            events.emit("rejected", name=name, error=str(ex)[:300])
            self.say(str(ex), "error")
        self.suggester.submitted(line, self.last_edit)
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
        self.refine_view(rid)  # retrievals from other front ends (mcp, agents) get a reading view too

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
            self.focus_id, self.last_edit = res.created[0], res.reg_id
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
            "s", "exa", "deep", "ls", "supersede", "contest", "reopen") else rest.split()

        if cmd in ("q", "quit"):
            return self.exit()
        if cmd == "help":
            return self.say(HELP, "body")
        if cmd in ("setup", "onboard"):
            return self.action_setup()
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
        if cmd in ("s", "exa", "deep", "fetch"):
            if not rest:
                raise ValueError(f"/{cmd} needs a query")
            tool = {"s": "search", "exa": "exa.search", "deep": "exa.deep", "fetch": "exa.fetch"}[cmd]
            # gate check up front so the UI refuses instantly
            if h.gateway.pending():
                raise GateBlocked(h.gateway.pending())
            self.say(f"… {tool}: {rest}")
            return self.run_search(tool, rest)
        if cmd == "ls":
            e, res = h.gateway.call("lobby.search", actor="user", query=rest)
            return self.render_results(e.id)
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
            m = re.match(r"^(?:R:)?(\d+)(?:#c(\d+))?$", args[0] if args else "")
            if not m or (args[1:] and args[1] not in ("all", "raw")):
                raise ValueError("/blob R:n[#ck] [all|raw]")
            return self.show_blob(int(m.group(1)), int(m.group(2)) if m.group(2) else None, args[1] if args[1:] else "")
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
            self.render_results(entry.id)
            self.say(f"{entry.rid} stored ({len(res.data)} hits, text {entry.text_hash[:10]}). "
                     f"gate locked — ctrl+a to annotate.", "warning")
            self.refresh_all()
            self.refine_view(entry.id)
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
            h.events.emit("error", where="guest", error=f"{type(ex).__name__}: {ex}"[:300])
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
            h.events.emit("error", where="research", error=f"{type(ex).__name__}: {ex}"[:300])
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
            h.events.emit("error", where="librarian", error=f"{type(ex).__name__}: {ex}"[:300])
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
