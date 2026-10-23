"""Reading view of a stored retrieval: fold, style and highlight it, never rewrite it.

The stored text blob is what annotation quotes are checked against, so it stays
byte for byte what the provider returned. The view only decides which of its
lines to fold away and how to style the rest:

  chrome   page furniture: navigation, button and link labels, sign-in and
           cookie prompts, share widgets, edit timestamps, a
           repeated page title. A deterministic rule finds the obvious cases;
           a model (Mercury 3 by default) can add more. The model sees numbered
           lines and may only answer with line numbers, so it has no way to add,
           reword or drop a word. Its verdict is cached in the Reg as an
           `internal` row (`reading.chrome`): line numbers only, no content.
  code     long code blocks, folded after their first lines.

Folded lines are one keypress away. Text copied from the view still quotes:
the view hides markdown marks, and gateway.find_quote ignores them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from rich.style import Style
from rich.text import Text

TOOL = "reading.chrome"
READER_V = 2  # bump when the prompt changes: older cached verdicts are redone
HIT_RE = re.compile(r"^### \[(\d+)\] (\S+?):(\S+) \((\w+)\)(?: (.*))?$")
META_RE = re.compile(r"^(url|published|query): (.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
INLINE_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|`([^`]+)`")
CODE_KEEP = 6  # lines of a long code block that stay visible
CODE_FOLD_OVER = 12  # blocks longer than this get folded
OWN_SOURCES = {"lobby"}  # the user's own notes are never chrome
BOLD = Style(bold=True)

UI_LABELS = {
    "sign in", "sign up", "log in", "login", "log out", "register", "subscribe", "unsubscribe", "share", "tweet",
    "menu", "open menu", "close menu", "toggle navigation", "navigation", "search", "skip to content",
    "skip to main content", "accept", "accept all", "accept cookies", "reject all", "cookie settings",
    "manage cookies", "cookies", "privacy policy", "terms of service", "terms of use", "read more", "learn more",
    "show more", "see more", "load more", "continue reading", "back to top", "home", "next", "previous", "prev",
    "print", "email", "copy link", "copy", "copied", "follow us", "advertisement", "sponsored", "related articles",
    "related posts", "you may also like", "edit", "reply", "like", "comments", "close", "download", "get started",
    "try it free", "contact us", "about us", "newsletter", "all rights reserved", "cookie policy", "sign in to comment",
    "view all", "see all", "share this", "share on twitter", "share on facebook", "share on linkedin", "ask ai",
}
TS_RE = re.compile(r"^(edit(ed)?|updated|posted|published|last (edited|updated))\b.{0,16}\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", re.I)
CRUMB_RE = re.compile(r"^(?:[^|·•»>]{1,30}\s[|·•»>]\s){2,}[^|·•»>]{1,30}$")


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


# ---------------------------------------------------------------- structure
@dataclass
class Line:
    n: int
    start: int
    end: int  # exclusive, without the newline
    kind: str  # top | head | meta | blank | body
    hit: int  # 0 before the first hit
    code: bool = False


@dataclass
class Hit:
    n: int
    source: str
    id: str
    flag: str
    title: str
    url: str | None = None
    published: str | None = None
    query: str | None = None
    lines: list[int] = field(default_factory=list)  # body and blank lines


@dataclass
class Doc:
    text: str
    lines: list[Line]
    hits: dict[int, Hit]

    def line_text(self, n: int) -> str:
        ln = self.lines[n]
        return self.text[ln.start:ln.end]

    def hit_at(self, offset: int) -> int:
        for ln in self.lines:
            if ln.start <= offset <= ln.end:
                return ln.hit
        return 0


def parse(text: str) -> Doc:
    """Lines of a rendered retrieval (search.render_hits), each tagged with its hit and role."""
    lines: list[Line] = []
    hits: dict[int, Hit] = {}
    pos, hit, in_meta, fence = 0, 0, False, False
    for n, raw in enumerate(text.split("\n")):
        start, end = pos, pos + len(raw)
        pos = end + 1
        code = False
        if m := HIT_RE.match(raw):
            hit, in_meta, fence = int(m.group(1)), True, False
            hits[hit] = Hit(hit, m.group(2), m.group(3), m.group(4), (m.group(5) or "").strip())
            kind = "head"
        elif not raw.strip():
            kind, in_meta = "blank", False
            code = fence
        elif hit == 0:
            kind = "top"
        elif in_meta and (mm := META_RE.match(raw)):
            kind = "meta"
            setattr(hits[hit], mm.group(1), mm.group(2).strip())
        else:
            kind, in_meta = "body", False
            if raw.strip().startswith("```"):
                code, fence = True, not fence
            else:
                code = fence
        lines.append(Line(n, start, end, kind, hit, code))
        if hit and kind in ("body", "blank"):
            hits[hit].lines.append(n)
    return Doc(text, lines, hits)


# ---------------------------------------------------------------- chrome
def rule_chrome(doc: Doc) -> set[int]:
    """Lines that are page chrome beyond reasonable doubt. Conservative: what it misses, the model may add."""
    out: set[int] = set()
    for h in doc.hits.values():
        if h.source in OWN_SOURCES:
            continue
        body = [n for n in h.lines if doc.lines[n].kind == "body" and not doc.lines[n].code]
        title = _norm(h.title)
        for i, n in enumerate(body):
            raw = doc.line_text(n).strip()
            t = _norm(raw.lstrip("-*•>#| "))
            if not t:
                continue
            if (t in UI_LABELS or TS_RE.match(raw) or (len(raw) <= 60 and (raw.startswith("©") or t.startswith("copyright ")))
                    or (i < 2 and title and (t == title or (len(t) > 8 and title.startswith(t))))
                    or (not raw.startswith("|") and CRUMB_RE.match(raw) and not re.search(r"[.!?]", raw))):
                out.add(n)
        # runs of four or more very short label-like lines are menus
        run: list[int] = []
        for n in body + [-1]:
            raw = doc.line_text(n).strip() if n >= 0 else ""
            short = (n >= 0 and len(raw.split()) <= 3 and not re.search(r"[.!?:;,]$", raw)
                     and not raw.startswith(("#", "-", "*", "|", ">")) and not raw[:1].isdigit())
            contiguous = run and n >= 0 and all(doc.lines[k].kind == "blank" for k in range(run[-1] + 1, n))
            if short and (not run or contiguous):
                run.append(n)
                continue
            if len(run) >= 4:
                out.update(run)
            run = [n] if short else []
    return out


def candidates(doc: Doc, limit: int = 600) -> list[int]:
    """Lines a model may mark: non-empty prose lines of external hits (code is folded anyway)."""
    out = [ln.n for ln in doc.lines if ln.kind == "body" and not ln.code and ln.hit
           and doc.hits[ln.hit].source not in OWN_SOURCES]
    return out[:limit]


READER_PROMPT = """You tidy web search results for reading. You get numbered lines of extracted page text, grouped by result. Mark the lines that are page chrome: navigation and menus, button and link labels ("Sign in", "Make your first request", "Read more"), cookie or consent text, share and follow widgets, timestamps such as "Edit 2022-06-20 20:21", a page title repeated as a line, ads, newsletter boxes, footers, breadcrumbs, and lists of links to other pages.
Never mark content: usernames, author names and bylines (they show who is speaking, e.g. in a forum thread), a sentence that says something, the article's own headings, list items or table rows that carry information, code, quotes. Off-topic, wrong or repeated content is still content. When unsure, do not mark the line.
Return JSON only: {"chrome": [line numbers]}"""

SCHEMA = {"name": "Chrome", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["chrome"],
    "properties": {"chrome": {"type": "array", "items": {"type": "integer"}}}}}


def model_prompt(doc: Doc, lines: list[int]) -> str:
    out, hit = [], None
    for n in lines:
        ln = doc.lines[n]
        if ln.hit != hit:
            hit = ln.hit
            h = doc.hits[hit]
            out.append(f"\n## result {hit}: {h.title or h.id}")
        out.append(f"{n}| {doc.line_text(n).strip()[:160]}")
    return "\n".join(out).strip()


def model_chrome(hotel, rid: int, profile: str | None = None, transport=None) -> set[int]:
    """Ask a model which lines are chrome; cache the line numbers in the Reg. Raises on a bad answer."""
    from .llm import LLM
    e = hotel.reg.get(rid)
    doc = parse(hotel.blobs.text(e.text_hash))
    cand = candidates(doc)
    picked: set[int] = set()
    name = None
    if cand:
        llm = LLM(hotel, profile, role="reader", transport=transport)
        name = llm.name
        _, msg = llm.chat([{"role": "system", "content": READER_PROMPT},
                           {"role": "user", "content": model_prompt(doc, cand)}],
                          json_schema=SCHEMA, max_tokens=4096, actor="reader")
        content = re.sub(r"^```(?:json)?|```$", "", (msg.get("content") or "").strip(), flags=re.M).strip()
        data = json.loads(content)
        marked = data if isinstance(data, list) else data.get("chrome")  # some models drop the wrapper
        if not isinstance(marked, list):
            raise ValueError("reader answer has no chrome list")
        picked = {x for x in marked if isinstance(x, int)} & set(cand)  # anything else is ignored
    hotel.gateway.log("internal", "reader", tool=TOOL, ref_id=rid, payload={
        "text_blob": e.text_hash, "v": READER_V, "lines": sorted(picked), "candidates": len(cand), "model": name})
    return picked


def model_profile(cfg: dict) -> str | None | bool:
    """reading_filter -> profile name (None = default profile), or False when the model is not wanted."""
    mode = cfg.get("reading_filter", "model")
    if mode in ("rule", "off", None, False):
        return False
    return None if mode == "model" else mode


def _fresh(row, e) -> bool:
    p = row.payload or {}
    return p.get("text_blob") == e.text_hash and p.get("v") == READER_V


def needs_model(hotel, rid: int) -> bool:
    from .llm import unavailable
    prof = model_profile(hotel.cfg)
    e = hotel.reg.get(rid)
    if prof is False or not e or e.kind != "retrieval" or not e.text_hash:
        return False
    row = hotel.reg.latest("internal", TOOL, rid)
    if row and _fresh(row, e):
        return False
    try:
        doc = parse(hotel.blobs.text(e.text_hash))
    except FileNotFoundError:
        return False
    return bool(candidates(doc)) and unavailable(hotel.cfg, prof) is None


def chrome_for(hotel, rid: int, doc: Doc) -> tuple[set[int], str]:
    """(chrome lines, who found them): the rule, plus the cached model verdict when there is one."""
    if hotel.cfg.get("reading_filter", "model") == "off":
        return set(), "off"
    found = rule_chrome(doc)
    e = hotel.reg.get(rid)
    row = hotel.reg.latest("internal", TOOL, rid)
    if row and e and _fresh(row, e):
        return found | set(row.payload.get("lines") or []), row.payload.get("model") or "model"
    return found, "rule"


# ---------------------------------------------------------------- folding
@dataclass
class Fold:
    first: int
    last: int
    label: str
    quiet: bool = False  # a line or two of chrome: hidden without a marker (the marker would be as long)


def folds(doc: Doc, chrome: set[int]) -> list[Fold]:
    out: list[Fold] = []
    for h in doc.hits.values():
        group: list[int] = []
        for n in h.lines + [-1]:
            ln = doc.lines[n] if n >= 0 else None
            if ln and ln.kind == "blank" and group:
                continue
            if ln and n in chrome:
                group.append(n)
                continue
            if group:
                k = len(group)
                out.append(Fold(group[0], group[-1], f"{k} lines of page chrome", quiet=k <= 2))
                group = []
        block: list[int] = []
        for n in h.lines + [-1]:
            ln = doc.lines[n] if n >= 0 else None
            if ln and ln.code:
                block.append(n)
                continue
            if len(block) > CODE_FOLD_OVER + 2:  # + the two fence lines
                hidden = block[1 + CODE_KEEP:-1]
                out.append(Fold(hidden[0], hidden[-1], f"{len(hidden)} more lines of code"))
            block = []
    return sorted(out, key=lambda f: f.first)


# ---------------------------------------------------------------- rendering
class _Out:
    """A Rich Text plus, for every piece copied verbatim from the blob, where it came from,
    so spans of the stored text (quotes) can be highlighted in the view."""

    def __init__(self):
        self.t = Text()
        self.segs: list[tuple[int, int, int]] = []  # (blob offset, view offset, length)

    def add(self, s: str, style=None, src: int | None = None):
        if src is not None and s:
            self.segs.append((src, len(self.t), len(s)))
        self.t.append(s, style)

    def stylize(self, start: int, end: int, style):
        for src, at, n in self.segs:
            a, b = max(start, src), min(end, src + n)
            if a < b:
                self.t.stylize(style, at + a - src, at + b - src)


def _inline(o: _Out, s: str, at: int, st: dict, base):
    """A prose line with **bold**, __bold__ and `code` marks hidden and their contents styled."""
    pos = 0
    for m in INLINE_RE.finditer(s):
        o.add(s[pos:m.start()], base, at + pos)
        g = 1 if m.group(1) is not None else 2 if m.group(2) is not None else 3
        style = st["string"] if g == 3 else (base or Style()) + BOLD
        o.add(m.group(g), style, at + m.start(g))
        pos = m.end()
    o.add(s[pos:], base, at + pos)


def _head(o: _Out, h: Hit, st: dict):
    o.add(f"[{h.n}] ", st["number"])
    o.add((h.title or h.id) + "\n", st["room_name"])
    bits = [h.source]
    if h.url:
        bits.append(urlparse(h.url).netloc.removeprefix("www.") or h.url)
    if h.published:
        bits.append(h.published[:10])
    o.add("    ")
    o.add(" · ".join(bits), st["muted"])
    flag_st = {"external": "warning", "own": "created", "secondhand": "hint"}.get(h.flag, "muted")
    o.add(f" · {h.flag}", st[flag_st])
    if h.query:
        o.add(f" · query: {h.query}", st["muted"])
    o.add("\n")
    if h.url:
        o.add(f"    {h.url}\n", st["meta_ref"])


def render(doc: Doc, st: dict, chrome: set[int] = frozenset(), *, show_all: bool = False,
           highlights: list[tuple[int, int, object]] = (), only_hit: int | None = None) -> Text:
    """The readable view. show_all unfolds everything (chrome is then dimmed, not hidden);
    highlights are (start, end, style) spans of the stored text; only_hit renders one result."""
    o = _Out()
    fold_at = {f.first: f for f in folds(doc, chrome)}
    skip_to = -1
    blank = True  # collapse runs of blank lines; none at the very top
    for ln in doc.lines:
        if only_hit is not None and ln.hit != only_hit:
            continue
        raw = doc.line_text(ln.n)
        if ln.n <= skip_to:
            continue
        if ln.kind == "blank" or ln.kind == "meta":
            if ln.kind == "blank" and not blank:
                o.add("\n")
                blank = True
            continue
        if ln.kind == "top":
            k, _, v = raw.partition(": ")
            if k == "QUERY":
                o.add("query  ", st["muted"])
                o.add(v + "\n", st["room_name"])
            elif k == "RETRIEVED":
                o.add(f"retrieved {v}\n", st["muted"])
            else:
                o.add(raw, st["body"], ln.start)
                o.add("\n")
            blank = False
            continue
        if ln.kind == "head":
            if not blank:
                o.add("\n")
            _head(o, doc.hits[ln.hit], st)
            o.add("\n")
            blank = True
            continue
        f = fold_at.get(ln.n)
        if f and not show_all:
            if not f.quiet:
                o.add(f"  ⋯ {f.label}\n", st["hint"])
                blank = False
            skip_to = f.last
            continue
        blank = False
        dim = show_all and ln.n in chrome
        o.add("  ")
        if dim:
            o.add(raw, st["dead"], ln.start)
        elif ln.code:
            o.add(raw, st["muted"] if raw.strip().startswith("```") else st["string"], ln.start)
        elif m := HEADING_RE.match(raw):
            o.add(m.group(2), st["type"] + BOLD, ln.start + m.start(2))
        else:
            lead = len(raw) - len(raw.lstrip())
            body = raw[lead:]
            o.add(raw[:lead], None, ln.start)
            if body.startswith(("- ", "* ", "• ")):
                o.add(body[:2], st["accent"], ln.start + lead)
                _inline(o, body[2:], ln.start + lead + 2, st, st["body"])
            else:
                _inline(o, body, ln.start + lead, st, st["body"])
        o.add("\n")
    for s, e, style in highlights:
        o.stylize(s, e, style)
    return o.t


def preview(doc: Doc, st: dict, chrome: set[int] = frozenset(), chars: int = 280) -> Text:
    """The Results tab: each hit's header and the start of its content, chrome and code skipped."""
    o = _Out()
    hidden = {n for f in folds(doc, chrome) for n in range(f.first, f.last + 1)} | chrome
    for h in doc.hits.values():
        _head(o, h, st)
        words, size = [], 0
        for n in h.lines:
            ln = doc.lines[n]
            if ln.kind != "body" or ln.code or n in hidden:
                continue
            raw = doc.line_text(n).strip()
            m = HEADING_RE.match(raw)
            s = " ".join(INLINE_RE.sub(lambda x: x.group(1) or x.group(2) or x.group(3), m.group(2) if m else raw).split())
            words.append(s)
            size += len(s) + 1
            if size >= chars:
                break
        snip = " ".join(words)
        o.add("    " + (snip[:chars] + ("…" if len(snip) > chars else "") if snip else "(nothing but page chrome)") + "\n\n",
              st["body"] if snip else st["muted"])
    if not doc.hits:
        o.add("no results\n", st["muted"])
    return o.t
