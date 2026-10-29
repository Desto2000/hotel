"""Lobby grammar — a rigid, line-oriented plain-text format.

    #! hotel-lobby v1

    == ROOM: harness ==

    @L0001 [ ] bounty origin=user ts=2026-10-19T10:02:11Z
      | Does Exa's search endpoint support a published-date filter?
      + tag: exa
      + ref: R:12

Rules
  * line 1 is exactly the header
  * `== ROOM: <slug> ==` opens a room (room = topic); each room appears once
  * an entry is a header line, >=1 body lines (`  | text`), then 0+ meta
    lines (`  + key: value`); a blank line ends the entry
  * sigils: [ ] open  [x] closed  [~] superseded  [!] contested  [.] static
  * lifecycle types (bounty, goal, idea) live in { ,x,~,!}
    static types (monologue, reasoning, log) live in {.,~}
  * origin: user | approved (user-approved model draft) | resident
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

HEADER = "#! hotel-lobby v1"

ROOM_RE = re.compile(r"^== ROOM: ([a-z0-9][a-z0-9_\-/.]*) ==$")
ENTRY_RE = re.compile(
    r"^@(L\d{4,}) \[([ x~!.])\] ([a-z]+) origin=([a-z]+) ts=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)$"
)
BODY_RE = re.compile(r"^  \|(?: (.*))?$")
META_RE = re.compile(r"^  \+ ([a-z][a-z\-]*): (\S.*)$")

TYPES = {"monologue", "bounty", "goal", "reasoning", "idea", "log"}
LIFECYCLE = {"bounty", "goal", "idea"}
STATIC = {"monologue", "reasoning", "log"}
ORIGINS = {"user", "approved", "resident"}
SIGIL_NAMES = {" ": "open", "x": "closed", "~": "superseded", "!": "contested", ".": "static"}
TRANSITIONS = {" ": {"x", "~", "!"}, "!": {" ", "x", "~"}, "x": {"~"}, ".": {"~"}, "~": set()}

ID_RE = re.compile(r"^L\d{4,}$")
REF_RE = re.compile(r"^(R:\d+|L\d{4,}|RAG:\d+)(#[\w.\-:]+)?$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
META_VALUE = {
    "tag": TAG_RE,
    "ref": REF_RE,
    "link": ID_RE,
    "supersedes": ID_RE,
    "superseded-by": ID_RE,
    "closed-by": re.compile(r"^(R:\d+|L\d{4,})$"),
    "promoted": re.compile(r"^RAG:\d+$"),
    "revalidate": re.compile(r"^\d{4}-\d\d-\d\d$"),
    "moved-from": re.compile(r"^[a-z0-9][a-z0-9_\-/.]*$"),
    "note": re.compile(r"^.+$"),
}
ROOM_SLUG = re.compile(r"^[a-z0-9][a-z0-9_\-/.]*$")


def creation_sigil(entry_type: str) -> str:
    return " " if entry_type in LIFECYCLE else "."


def allowed_sigils(entry_type: str) -> set[str]:
    return {" ", "x", "~", "!"} if entry_type in LIFECYCLE else {".", "~"}


@dataclass
class Entry:
    id: str
    room: str
    sigil: str
    type: str
    origin: str
    ts: str
    body: list[str] = field(default_factory=list)
    meta: list[tuple[str, str]] = field(default_factory=list)
    start: int = 0  # line index of header (0-based)
    end: int = 0  # line index of last line of the entry

    @property
    def num(self) -> int:
        return int(self.id[1:])

    @property
    def status(self) -> str:
        return SIGIL_NAMES[self.sigil]

    @property
    def text(self) -> str:
        return "\n".join(self.body)

    def metas(self, key: str) -> list[str]:
        return [v for k, v in self.meta if k == key]

    @property
    def is_live(self) -> bool:
        return self.sigil in (" ", "!", ".")


@dataclass
class Lobby:
    """A parsed lobby. `parse` caches and shares these: treat them as read-only
    (ops copy `lines` before editing)."""
    lines: list[str]
    rooms: list[str]
    room_line: dict[str, int]
    entries: dict[str, Entry]
    by_room: dict[str, list[Entry]] = field(default_factory=dict)
    _max_num: int = 0

    def in_room(self, room: str) -> list[Entry]:
        return list(self.by_room.get(room, ()))

    def max_num(self) -> int:
        return self._max_num

    def next_id(self, offset: int = 1) -> str:
        return f"L{self.max_num() + offset:04d}"


class LobbyParseError(Exception):
    def __init__(self, errors: list[tuple[int, str]]):
        self.errors = errors
        super().__init__("; ".join(f"line {n}: {m}" for n, m in errors[:8]))


def split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


@lru_cache(maxsize=8)
def parse(text: str) -> Lobby:
    """Parse a lobby text. Cached by text: one write validates, applies and indexes the same
    text several times, and the TUI re-reads the lobby on every refresh. Raises
    LobbyParseError (never cached) for invalid text."""
    lines = split_lines(text)
    errors: list[tuple[int, str]] = []
    if not lines or lines[0] != HEADER:
        errors.append((1, f"first line must be {HEADER!r}"))
    rooms: list[str] = []
    room_line: dict[str, int] = {}
    entries: dict[str, Entry] = {}
    room: str | None = None
    cur: Entry | None = None
    phase = None  # head | body | meta

    for i, line in enumerate(lines[1:], start=1):
        ln = i + 1
        if line == "":
            cur = None
            continue
        if "\t" in line or line[-1].isspace():
            errors.append((ln, "tabs / trailing whitespace not allowed"))
            continue
        # Body lines are most of the file: test them by prefix, without a regex
        # (same language as BODY_RE). Other kinds go to their own regex by prefix.
        if line.startswith("  |") and (len(line) == 3 or line[3] == " "):
            if cur is None or phase not in ("head", "body"):
                errors.append((ln, "body line outside an entry header/body"))
                continue
            cur.body.append(line[4:])
            cur.end, phase = i, "body"
            continue
        if line.startswith("== ") and (m := ROOM_RE.match(line)):
            name = m.group(1)
            if name in room_line:
                errors.append((ln, f"room {name!r} already declared on line {room_line[name] + 1}"))
            else:
                rooms.append(name)
                room_line[name] = i
            room, cur = name, None
            continue
        if line[0] == "@" and (m := ENTRY_RE.match(line)):
            eid, sigil, etype, origin, ts = m.groups()
            if room is None:
                errors.append((ln, "entry outside of any room"))
            if eid in entries:
                errors.append((ln, f"duplicate id {eid}"))
            if etype not in TYPES:
                errors.append((ln, f"unknown type {etype!r}"))
            elif sigil not in allowed_sigils(etype):
                errors.append((ln, f"sigil [{sigil}] not allowed for type {etype}"))
            if origin not in ORIGINS:
                errors.append((ln, f"unknown origin {origin!r}"))
            if cur is not None:
                errors.append((ln, "entries must be separated by a blank line"))
            cur = Entry(eid, room or "?", sigil, etype, origin, ts, start=i, end=i)
            entries.setdefault(eid, cur)
            phase = "head"
            continue
        if line.startswith("  + ") and (m := META_RE.match(line)):
            key, val = m.groups()
            if cur is None or phase not in ("body", "meta"):
                errors.append((ln, "meta line must follow an entry body"))
                continue
            if key not in META_VALUE:
                errors.append((ln, f"unknown meta key {key!r}"))
            elif not META_VALUE[key].match(val):
                errors.append((ln, f"bad value for {key}: {val!r}"))
            cur.meta.append((key, val))
            cur.end, phase = i, "meta"
            continue
        errors.append((ln, f"unrecognised line: {line[:60]!r}"))

    for e in entries.values():
        if not e.body:
            errors.append((e.start + 1, f"{e.id} has no body"))
    if errors:
        raise LobbyParseError(errors)
    by_room: dict[str, list[Entry]] = {}
    for e in entries.values():
        by_room.setdefault(e.room, []).append(e)
    return Lobby(lines, rooms, room_line, entries, by_room, max((e.num for e in entries.values()), default=0))


def render_entry(
    eid: str, sigil: str, etype: str, origin: str, ts: str, body: str, meta: list[tuple[str, str]]
) -> list[str]:
    body = body.replace("\r\n", "\n").replace("\t", "    ")
    if not body.strip():
        raise ValueError("entry body may not be empty")
    out = [f"@{eid} [{sigil}] {etype} origin={origin} ts={ts}"]
    for bl in body.strip("\n").split("\n"):
        bl = bl.rstrip()
        out.append(f"  | {bl}" if bl else "  |")
    out += [f"  + {k}: {v}" for k, v in meta]
    return out


def empty_lobby() -> str:
    return HEADER + "\n"
