"""Grammar-aware highlighting of lobby text into Rich Text (Calculus palette)."""

from __future__ import annotations

from rich.text import Text

from .grammar import BODY_RE, ENTRY_RE, HEADER, META_RE, ROOM_RE

BUILDING = [  # "#" = a window, "=" = the sign's neon; the last row is the street
    "      ┌───────────┐      ",
    "      │ ========= │      ",
    "    ┌─┴───────────┴─┐    ",
    "    │ # # # # # # # │    ",
    "    │ # # # # # # # │    ",
    "    │ # # # # # # # │    ",
    "    │ # # # # # # # │    ",
    "    │ # # # # # # # │    ",
    "    │ # # # ┌─┐ # # │    ",
    "────┴───────┴─┴─────┴────",
]
WINDOWS = [(r, c) for r, row in enumerate(BUILDING) for c, ch in enumerate(row) if ch == "#"]


def building(st: dict, top: int = 0, street_k: int = len(BUILDING[-1]), lit: set | None = None,
             sign: bool = True) -> Text:
    """One frame of the hotel (the TUI's opening animation; `hotel log`'s banner).
    Rows above `top` are not built yet; the street is drawn `street_k` cells either side
    of the middle; `lit` windows glow (None: all of them)."""
    t = Text()
    street = len(BUILDING) - 1
    mid = len(BUILDING[-1]) // 2
    lit = set(WINDOWS) if lit is None else lit
    for r, row in enumerate(BUILDING):
        for c, ch in enumerate(row):
            if r == street:
                t.append(("─" if top == street else ch) if abs(c - mid) < street_k else " ", st["muted"])
            elif r < top:
                t.append(" ")
            elif ch == "#":
                t.append("■", st["warning"] if (r, c) in lit else st["dead"])
            elif ch == "=":
                t.append("▬", st["room_name"] if sign else st["dead"])
            else:
                t.append(ch, st["muted"])
        t.append("\n")
    t.rstrip()
    return t


SIG_STYLE = {" ": "sig_open", "x": "sig_closed", "~": "sig_superseded", "!": "sig_contested", ".": "sig_static"}


def highlight_lobby(text: str, st: dict, focus: str | None = None, gutter: bool = True) -> tuple[Text, dict[str, int]]:
    out = Text(no_wrap=True, overflow="ellipsis")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    width = len(str(len(lines)))
    positions: dict[str, int] = {}
    dead = False
    in_focus = False
    for i, line in enumerate(lines):
        row = Text()
        if line == "":
            in_focus = False
        if m := ENTRY_RE.match(line):
            eid, sig, typ, origin, ts = m.groups()
            positions[eid] = i
            dead = sig == "~"
            in_focus = eid == focus
            row.append("@", st["at"])
            row.append(eid, st["id"])
            row.append(" [", st["bracket"])
            row.append(sig, st[SIG_STYLE[sig]])
            row.append("] ", st["bracket"])
            row.append(typ, st["dead"] if dead else st["type"])
            row.append(" origin=", st["key"])
            row.append(origin, st.get(f"origin_{origin}", st["body"]))
            row.append(" ts=", st["key"])
            row.append(ts, st["ts"])
        elif m := ROOM_RE.match(line):
            dead = False
            row.append("== ", st["room_punct"])
            row.append("ROOM: ", st["room_kw"])
            row.append(m.group(1), st["room_name"])
            row.append(" ==", st["room_punct"])
        elif BODY_RE.match(line):
            row.append("  |", st["pipe"])
            row.append(line[3:], st["dead"] if dead else st["body"])
        elif m := META_RE.match(line):
            k, v = m.groups()
            row.append("  + ", st["plus"])
            row.append(k, st["meta_key"])
            row.append(": ", st["pipe"])
            vs = {"tag": "meta_tag", "note": "meta_note"}.get(k, "meta_ref")
            row.append(v, st["dead"] if dead else st[vs])
        elif line == HEADER:
            row.append(line, st["header"])
        else:
            row.append(line, st["error"])
        if in_focus:
            row.stylize(st["active_line"])
        if gutter:
            g = Text(f"{i + 1:>{width}} ", st["gutter_active"] if in_focus else st["gutter"])
            out.append(g)
        out.append(row)
        out.append("\n")
    return out, positions
