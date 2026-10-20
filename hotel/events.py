"""Event log: one JSON line per thing that happens, so `hotel log` can show a live timeline.

The Reg is the record; this file is the timeline. Every Reg row is mirrored here, plus
what the Reg does not hold: processes starting and exiting, sessions, TUI commands and
the errors they hit. Lines carry metadata only (ids, kinds, tools, timings, tokens,
errors). Queries, results and lobby text stay in blobs, where `hotel forget` can
delete them.

Every process (TUI, MCP server, CLI) appends to the same file. A follower prints it
as it grows.
"""

from __future__ import annotations

import json
import os
import random
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.style import Style
from rich.text import Text

# Reg payload keys that are metadata, not content; anything else stays in the Reg.
REG_META = ("ms", "model", "tokens_in", "tokens_out", "cost_usd", "bounty", "turns", "output",
            "verdict", "commit", "claims", "pending", "created", "hand_edit")


class EventLog:
    def __init__(self, path: Path, source: str = "hotel"):
        self.path = path
        self.source = source  # which front end wrote the line: tui, mcp, research, ...
        self._lock = threading.Lock()

    def emit(self, event: str, **fields) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "src": self.source,
               "pid": os.getpid(), "event": event, **{k: v for k, v in fields.items() if v is not None}}
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # the timeline must never break the thing it observes

    def reg_row(self, e) -> None:
        p = e.payload if isinstance(e.payload, dict) else {}
        self.emit("reg", id=e.id, kind=e.kind, actor=e.actor, tool=e.tool, provider=e.provider,
                  ref=e.ref_id, status=e.status, error=e.error, session=e.session_id,
                  **{k: p[k] for k in REG_META if p.get(k) is not None})


# ---------------------------------------------------------------- rendering

def _dur(ms: int | None) -> str:
    if ms is None:
        return ""
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def _since(seconds: float) -> str:
    s = int(seconds)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m"


def describe(r: dict) -> tuple[str, str, str]:
    """(glyph, style key, message) for one record."""
    ev = r.get("event")
    if ev == "start":
        return "·──●", "created", f"Started hotel {r.get('cmd')} (pid {r.get('pid')})"
    if ev == "exit":
        took = f" after {_since(r['seconds'])}" if r.get("seconds") is not None else ""
        if r.get("error"):
            return "●──✕", "error", f"Crashed{took}: {r['error']}"
        if r.get("code"):
            return "●──·", "warning", f"Exited with code {r['code']}{took}"
        return "●──·", "muted", f"Exited{took}"
    if ev == "session":
        return "  ◆", "accent", f"New session {r.get('id')} ({r.get('label') or 'unlabelled'})"
    if ev == "command":
        return "  ›", "hint", f"Command {r.get('name')}" + (f" in room {r['room']}" if r.get("room") else "")
    if ev == "rejected":
        return "  !", "warning", f"{r.get('name')} rejected: {r.get('error')}"
    if ev == "error":
        return "  ✕", "error", f"{r.get('where')} failed: {r.get('error')}"
    if ev != "reg":
        return "  ?", "muted", json.dumps(r, ensure_ascii=False)

    rid, kind, actor, tool = f"R:{r.get('id')}", r.get("kind"), r.get("actor"), r.get("tool") or ""
    bits = [b for b in (_dur(r.get("ms")),) if b]
    if kind == "retrieval":
        glyph, sty, msg = "  ●", "function", f"{actor} retrieved via {tool} → {rid}"
    elif kind == "model":
        glyph, sty, msg = "  ◇", "number", f"{actor} called {r.get('model') or tool} → {rid}"
        if r.get("tokens_in") is not None:
            bits.append(f"{r['tokens_in']}→{r.get('tokens_out')} tok")
        if r.get("cost_usd") is not None:
            bits.append(f"${r['cost_usd']:.4f}")
    elif kind == "annotation":
        glyph, sty, msg = "  ✓", "success", f"{actor} annotated R:{r.get('ref')} ({r.get('claims', 0)} claims) → {rid}"
    elif kind == "lobby_edit":
        made = [i for ids in r.get("created") or [] for i in (ids if isinstance(ids, list) else [ids])]
        what = "hand edit" if r.get("hand_edit") else "lobby edit"
        glyph, sty, msg = "  ▲", "created", f"{actor} {what}" + (f" {' '.join(made)}" if made else "") + f" → {rid}"
        if r.get("commit"):
            bits.append(f"commit {r['commit'][:8]}")
    elif kind == "review":
        glyph, sty, msg = "  ?", "modified", f"{actor} drafted {rid} for review"
    elif kind == "decision":
        v = r.get("verdict") or "decided"
        glyph, sty, msg = "  ✓" if v == "approve" else "  ✕", "success" if v == "approve" else "deleted", \
            f"{actor} {v} review R:{r.get('ref')}"
    elif kind == "output":
        glyph, sty, msg = "  ■", "accent", f"{actor} wrote output {rid}"
    elif kind == "blocked":
        pend = " ".join(f"R:{p}" for p in r.get("pending") or [])
        glyph, sty, msg = "  ⛔", "warning", f"Gate blocked {tool} for {actor} (annotate {pend})"
    elif kind == "redaction":
        glyph, sty, msg = "  ✕", "deleted", f"{actor} forgot R:{r.get('ref')}"
    elif tool == "agent.start":
        glyph, sty, msg = "  ●", "hint", f"Agent started on {r.get('bounty')} ({r.get('model')}) → {rid}"
    elif tool == "agent.finish":
        glyph, sty, msg = "  ●", "hint", f"Agent finished {r.get('bounty')} in {r.get('turns')} turns"
        if r.get("output"):
            bits.append(f"output R:{r['output']}")
    else:
        glyph, sty, msg = "  ·", "muted", f"{actor} {kind} {tool} → {rid}"
    if r.get("status") not in (None, "ok", "blocked"):
        glyph, sty = "  ✕", "error"
        msg += f" {r['status']}"
        if r.get("error"):
            bits.append(r["error"])
    return glyph, sty, msg + (" · " + " · ".join(bits) if bits else "")


def render_line(r: dict, st: dict) -> Text:
    glyph, sty, msg = describe(r)
    t = Text()
    try:
        local = datetime.fromisoformat(r["ts"]).astimezone()
        t.append(local.strftime("%H:%M:%S") + " ", st["ts"])
    except (KeyError, ValueError):
        t.append("??:??:?? ", st["ts"])
    t.append(f"{glyph:<5}", st[sty])
    t.append(f"{(r.get('src') or '')[:8]:<9}", st["muted"])
    t.append(" ".join(msg.split()), st[sty] if sty in ("error", "warning") else st["body"])  # one line per event
    return t


def banner(paths, st: dict, follow: bool) -> Text:
    """The opening animation's last frame (lit hotel, sign on), with the facts beside its floors."""
    from . import __version__
    from .render import WINDOWS, building
    home = str(paths.root).replace(str(Path.home()), "~", 1)
    dark = set(random.Random(0).sample(WINDOWS, 4))  # a few stay dark, as in the TUI
    art = building(st, lit=set(WINDOWS) - dark).split("\n")
    side = {3: ("HOTEL LOG", st["room_name"]), 4: (__version__, st["muted"]),
            5: (home, st["body"]), 6: (socket.gethostname(), st["muted"])}
    t = Text()
    for r, row in enumerate(art):
        t.append_text(row)
        if r in side:
            t.append("    " + side[r][0], side[r][1])
        t.append("\n")
    t.append("\n")
    for k, v in (("Home", paths.root), ("Log", paths.log)):
        t.append(f"{k:<6}", st["body"])
        t.append(str(v).replace(str(Path.home()), "~", 1) + " ↗\n", Style(color=st["meta_ref"].color, underline=True,
                                                                          link=Path(v).resolve().as_uri()))
    t.append("Press Ctrl+C to stop.\n" if follow else "", st["muted"])
    return t


def _parse(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"event": "?", "raw": raw.decode("utf-8", "replace")}


def show(paths, styles: dict, *, last: int = 50, follow: bool = True, console: Console | None = None,
         poll: float = 0.25, stop=None) -> None:
    """Print the banner and the last `last` events; with follow, keep printing new ones."""
    out = console or Console(highlight=False)
    out.print(banner(paths, styles, follow))
    log, pos, day = paths.log, 0, None

    def emit_lines(chunk: bytes):
        nonlocal day
        for raw in chunk.splitlines():
            if not raw.strip():
                continue
            r = _parse(raw)
            try:
                d = datetime.fromisoformat(r["ts"]).astimezone().strftime("%a %d %b %Y")
            except (KeyError, ValueError):
                d = None
            if d and d != day:
                day = d
                out.print(Text(f"── {d} ──", styles["muted"]))
            out.print(render_line(r, styles), overflow="ellipsis", no_wrap=True)  # full text stays in the file

    if log.exists():
        data = log.read_bytes()
        end = data.rfind(b"\n") + 1  # a writer may be mid-line; leave the partial line for later
        emit_lines(b"\n".join(data[:end].splitlines()[-last:]) if last else b"")
        pos = end
    if not follow:
        return
    try:
        while not (stop and stop()):
            try:
                size = log.stat().st_size
            except FileNotFoundError:
                size = 0
            if size < pos:  # truncated or replaced
                pos = 0
            if size > pos:
                with open(log, "rb") as f:
                    f.seek(pos)
                    chunk = f.read(size - pos)
                end = chunk.rfind(b"\n") + 1
                if end:
                    emit_lines(chunk[:end])
                    pos += end
            time.sleep(poll)
    except KeyboardInterrupt:
        pass
