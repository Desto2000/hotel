"""hotel CLI. `hotel` with no args opens the TUI."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import keys
from .config import Paths, find_root
from .events import EventLog


_run: dict = {}  # the process's EventLog and start time, for the exit event


def _home(args) -> Paths:
    root = Path(args.home).resolve() if args.home else find_root()
    if root is None or not (root / ".hotel").is_dir():
        sys.exit("no hotel home found — run `hotel init <dir>`, `hotel home <dir>`, or set HOTEL_HOME")
    return Paths(root)


def _hotel(args, **kw):
    import time

    from .core import Hotel
    paths = _home(args)
    cmd = args.cmd or "tui"
    events = EventLog(paths.log, cmd)
    events.emit("start", cmd=cmd)  # before Hotel(): opening can start a session, which should follow
    _run.update(events=events, t0=time.monotonic())
    return Hotel(paths, source=cmd, **kw)


def doctor(args) -> int:
    """Check the install end to end; exit 1 on any hard failure."""
    import os
    import shutil
    import sqlite3

    from . import __version__
    from .config import USER_HOME_FILE
    ok = True

    def line(good: bool | None, what: str, detail: str = ""):
        nonlocal ok
        mark = {True: "ok  ", False: "FAIL", None: "warn"}[good]
        ok = ok and good is not False
        print(f"[{mark}] {what}{(' — ' + detail) if detail else ''}")

    line(True, f"hotel {__version__}", f"python {sys.version.split()[0]} at {sys.executable}")
    try:
        c = sqlite3.connect(":memory:")
        c.execute("create virtual table t using fts5(x)")
        line(True, "sqlite FTS5", sqlite3.sqlite_version)
    except Exception as ex:
        line(False, "sqlite FTS5", str(ex))
    line(shutil.which("git") is not None, "git on PATH", shutil.which("git") or "missing")
    root = Path(args.home).resolve() if args.home else find_root()
    src = ("--home" if args.home else "HOTEL_HOME" if os.environ.get("HOTEL_HOME")
           else f"default ({USER_HOME_FILE})" if root else "none")
    line(root is not None, "hotel home", f"{root} via {src}" if root else "run `hotel init <dir>`")
    if root:
        from .core import Hotel
        h = Hotel(Paths(root))
        probs = h.check()
        line(not probs, "lobby valid vs git HEAD", "; ".join(probs[:2]))
        chain = h.reg.verify_chain()
        line(not chain, "reg hash chain", f"{len(h.reg.list(limit=10**9))} rows" if not chain else chain[0])
        pend = h.gateway.pending()
        line(None if pend else True, "annotation gate", "locked by " + ", ".join(p.rid for p in pend) if pend else "open")
        from .backup import latest_snapshot
        bdir = Path(h.cfg.get("backup_dir") or "~/hotel-backups").expanduser()
        try:
            line(True, "latest backup", latest_snapshot(bdir).name)
        except FileNotFoundError:
            line(None, "latest backup", f"none under {bdir} — run `hotel backup`")
    for k in keys.status():
        what = f"{k['name']}{'' if k['required'] else ' (optional)'}"
        line(True if k["source"] else None, k["env"],
             f"{what}, from {k['source']}" if k["source"] else f"not set — {what} unavailable; run `hotel setup`")
    return 0 if ok else 1


def setup(args) -> int:
    home = None
    root = Path(args.home).resolve() if args.home else find_root()
    if root and (root / ".hotel").is_dir():
        home = Paths(root)
    if not keys.interactive():
        sys.exit("`hotel setup` is interactive; in scripts use `hotel keys set ENV_NAME` (reads stdin) or environment variables")
    done = keys.onboard(home=home, check=not args.no_check, only=args.only or None)
    if home is None:
        print("\nno hotel home yet — run `hotel init <dir>` to create one")
    return 0 if done else 1


def keys_cmd(args) -> int:
    if args.action == "list":
        for k in keys.status():
            print(f"{k['env']:<20} {k['source'] or 'missing':<8} {'required' if k['required'] else 'optional'}  {k['name']}")
        print(f"\nstore: {keys.KEYS_FILE}")
        return 0
    if not args.env:
        sys.exit(f"hotel keys {args.action} needs an ENV_NAME, e.g. EXA_API_KEY")
    if args.action == "rm":
        print("removed" if keys.remove_key(args.env) else "not in the key file")
        return 0
    value = getpass.getpass(f"{args.env}: ") if keys.interactive() else sys.stdin.readline()
    try:
        keys.set_key(args.env, value)
    except ValueError:
        sys.exit("empty key; nothing saved")
    print(f"saved {args.env} to {keys.KEYS_FILE}")
    return 0


def main(argv=None):
    """Run a command; if it opened a hotel, log how the process ended."""
    import time
    code, error = 0, None
    try:
        return _main(argv)
    except SystemExit as ex:
        code = ex.code if isinstance(ex.code, int) else (1 if ex.code else 0)
        raise
    except KeyboardInterrupt:
        code = 130
        raise
    except BaseException as ex:
        code, error = 1, f"{type(ex).__name__}: {ex}"[:500]
        raise
    finally:
        if ev := _run.pop("events", None):
            ev.emit("exit", code=code or None, error=error, seconds=round(time.monotonic() - _run["t0"], 1))


def _main(argv=None):
    p = argparse.ArgumentParser(prog="hotel")
    p.add_argument("--home", help="hotel home directory (default: HOTEL_HOME or nearest .hotel)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init"); s.add_argument("dir")
    s = sub.add_parser("tui"); s.add_argument("--new-session", action="store_true")
    sub.add_parser("session-new")
    s = sub.add_parser("add"); s.add_argument("room"); s.add_argument("type"); s.add_argument("text")
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--tool", default="search")
    s = sub.add_parser("annotate", help="annotate from a JSON file (Annotation schema)"); s.add_argument("file")
    s = sub.add_parser("lib"); s.add_argument("reg_id", type=int); s.add_argument("--librarian")
    s = sub.add_parser("review"); s.add_argument("action", choices=["list", "approve", "reject"]); s.add_argument("reg_id", nargs="?", type=int)
    sub.add_parser("check"); sub.add_parser("verify"); sub.add_parser("rebuild")
    s = sub.add_parser("reg"); s.add_argument("--limit", type=int, default=30)
    s = sub.add_parser("replay"); s.add_argument("session"); s.add_argument("--librarian", default="rule")
    s.add_argument("--approve-queued", action="store_true")
    s = sub.add_parser("rag", help="RAG store: import files/dirs (md, txt, html, pdf), list, forget")
    s.add_argument("action", choices=["import", "list", "forget"])
    s.add_argument("args", nargs="*", help="import: files or directories; forget: RAG id")
    s.add_argument("--title"); s.add_argument("--reason")
    s = sub.add_parser("rag-import", help="same as `hotel rag import`"); s.add_argument("file"); s.add_argument("--title")
    s = sub.add_parser("labels", help="Concierge training/eval data mined from the Reg, with retrieval quality")
    s.add_argument("--export", action="store_true", help="write .hotel/labels/*.jsonl + manifest.json")
    sub.add_parser("sessions")
    s = sub.add_parser("research", help="run the research agent on a bounty"); s.add_argument("entry")
    s.add_argument("--llm", help="router profile (default: config llm)"); s.add_argument("--max-searches", type=int)
    s = sub.add_parser("accept", help="keep an agent output as an approved lobby entry"); s.add_argument("reg_id", type=int)
    s.add_argument("--room")
    s = sub.add_parser("timings", help="model vs tool latency per agent run"); s.add_argument("--last", type=int, default=5)
    s = sub.add_parser("log", help="timeline of everything every hotel process did; follows live")
    s.add_argument("-n", "--last", type=int, default=50, help="past events to show first (default 50)")
    s.add_argument("--no-follow", action="store_true", help="print and exit instead of following")
    s = sub.add_parser("render", help="print the lobby (highlighted, or --plain / --directory)")
    s.add_argument("--plain", action="store_true"); s.add_argument("--directory", action="store_true")
    s = sub.add_parser("snapshot", help="run the TUI headless with scripted commands; dump text (+ svg)")
    s.add_argument("--cmd", dest="commands", action="append", default=[]); s.add_argument("--svg"); s.add_argument("--keys", action="append", default=[])
    s.add_argument("--size", default="190x46"); s.add_argument("--wait", type=float, default=0.5)
    sub.add_parser("schema", help="print the librarian JSON schema")
    sub.add_parser("version")
    sub.add_parser("doctor", help="check install, home, lobby, reg chain, backups, keys")
    s = sub.add_parser("setup", aliases=["onboard"], help="guided API key onboarding (validates and stores keys)")
    s.add_argument("--only", action="append", metavar="ENV_NAME", help="just this key (repeatable)")
    s.add_argument("--no-check", action="store_true", help="skip the live validation request")
    s = sub.add_parser("keys", help="list / set / rm stored API keys (set reads the value from stdin)")
    s.add_argument("action", choices=["list", "set", "rm"]); s.add_argument("env", nargs="?")
    s = sub.add_parser("home", help="show or set the default hotel home used from any directory")
    s.add_argument("dir", nargs="?")
    s = sub.add_parser("backup", help="incremental backup (reg, rag, blobs, lobby git bundle)")
    s.add_argument("dest", nargs="?")
    s = sub.add_parser("restore", help="restore the latest (or given) snapshot into an empty dir")
    s.add_argument("src"); s.add_argument("target"); s.add_argument("--snapshot")
    s = sub.add_parser("forget", help="irreversibly delete the content behind a Reg row (row stays)")
    s.add_argument("reg_id", type=int); s.add_argument("--reason", required=True)
    sub.add_parser("custodian", help="flag closed bounties whose evidence is due for revalidation")
    s = sub.add_parser("move", help="move a live entry to another room"); s.add_argument("entry"); s.add_argument("room")
    s.add_argument("--reason", default="refiled")
    s = sub.add_parser("guest", help="one-shot message to a guest (drafts go to the review queue)")
    s.add_argument("message"); s.add_argument("--llm"); s.add_argument("--note", action="store_true",
                                                                       help="also append your message as a monologue")
    s.add_argument("--room")
    s = sub.add_parser("mcp", help="serve the lobby to MCP clients (stdio; --http for remote clients)")
    s.add_argument("--http", action="store_true", help="streamable HTTP at http://HOST:PORT/mcp instead of stdio")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8765)
    s.add_argument("--token", help="bearer token for --http (default: $HOTEL_MCP_TOKEN); required off loopback")

    a = p.parse_args(argv)
    if a.cmd == "version":
        from . import __version__
        print(__version__)
        return
    if a.cmd == "doctor":
        sys.exit(doctor(a))
    if a.cmd in ("setup", "onboard"):
        sys.exit(setup(a))
    if a.cmd == "keys":
        sys.exit(keys_cmd(a))
    if a.cmd == "home":
        from .config import set_default_home
        if a.dir:
            d = Path(a.dir).expanduser().resolve()
            if not (d / ".hotel").is_dir():
                sys.exit(f"{d} is not a hotel home (no .hotel/); run `hotel init {d}` first")
            set_default_home(d)
        print(find_root() or "(no default home)")
        return
    if a.cmd == "restore":
        from .backup import restore
        info = restore(Path(a.src), Path(a.target), a.snapshot)
        print(json.dumps(info, indent=2))
        sys.exit(1 if info["problems"] else 0)
    if a.cmd == "init":
        from .config import USER_HOME_FILE, set_default_home
        from .core import init_home
        paths = init_home(Path(a.dir))
        print(f"initialised hotel home at {paths.root}")
        if not USER_HOME_FILE.exists():
            set_default_home(paths.root)
            print(f"set as default home ({USER_HOME_FILE}); `hotel` now works from any directory")
        left = keys.missing_required()
        if left and keys.interactive():
            if input(f"\n{', '.join(p.env for p in left)} not set. Set up API keys now? [Y/n] ").strip().lower() in ("", "y", "yes"):
                keys.onboard(home=paths)
        elif left:
            print("API keys missing: " + ", ".join(p.env for p in left) + " — run `hotel setup`")
        return
    if a.cmd == "log":
        from .config import load_config
        from .events import show
        from .theme import lobby_styles, palettes
        paths = _home(a)
        pal = palettes().get(load_config(paths).get("theme"), palettes()["calculus-dark"])
        show(paths, lobby_styles(pal), last=a.last, follow=not a.no_follow)
        return
    if a.cmd == "schema":
        from .models import LibrarianOutput
        print(json.dumps(LibrarianOutput.model_json_schema(), indent=2))
        return
    if a.cmd == "mcp":
        from .mcp_server import serve
        serve(_hotel(a, new_session=True, label="mcp"), a.http, a.host, a.port, a.token)
        return
    if a.cmd in (None, "tui"):
        from .tui import HotelApp
        h = _hotel(a, new_session=getattr(a, "new_session", False))
        left = keys.missing_required()
        if left and keys.interactive():
            print("missing API keys: " + ", ".join(p.env for p in left) + " — run `hotel setup` (search and model features are off until then)", file=sys.stderr)
        HotelApp(h).run()
        return

    h = _hotel(a, new_session=(a.cmd == "session-new"))
    if a.cmd == "session-new":
        print(h.session_id)
    elif a.cmd == "sessions":
        for s in h.reg.sessions():
            print(s["id"], s["started_at"], s["label"] or "")
    elif a.cmd == "add":
        from .models import AppendOp
        r = h.apply([AppendOp(op="append", room=a.room, type=a.type, body=a.text, refs=[], tags=[])])
        print(f"{r.created[0]}  commit {r.commit[:8]}  R:{r.reg_id}")
    elif a.cmd == "search":
        from .gateway import GateBlocked
        try:
            e, res = h.gateway.call(a.tool, actor="user", query=a.query)
        except GateBlocked as ex:
            sys.exit(str(ex))
        print(f"{e.rid}  text blob {e.text_hash}\n")
        print(h.blobs.text(e.text_hash))
    elif a.cmd == "annotate":
        from .models import Annotation
        ann = Annotation.model_validate_json(Path(a.file).read_text(encoding="utf-8"))
        print(h.gateway.annotate(ann).rid)
    elif a.cmd == "lib":
        from .librarian import get_librarian, run_librarian
        out, results = run_librarian(h, a.reg_id, get_librarian(h, a.librarian))
        print(out.rationale)
        for r in results:
            print(f"  {r.route:<8} {r.op.op} {getattr(r.op, 'id', '')}  {r.reason} {r.detail}")
    elif a.cmd == "review":
        if a.action == "list":
            for r in h.reg.pending_reviews():
                print(r.rid, json.dumps(r.payload, ensure_ascii=False))
        else:
            r = h.decide(a.reg_id, a.action == "approve")
            print(r.route, r.reason, r.detail)
    elif a.cmd == "check":
        errs = h.check()
        print("ok" if not errs else "\n".join(errs))
        sys.exit(1 if errs else 0)
    elif a.cmd == "verify":
        probs = h.reg.verify_chain()
        print("reg hash chain intact" if not probs else "\n".join(probs))
        sys.exit(1 if probs else 0)
    elif a.cmd == "rebuild":
        errs = h.rebuild()
        print("rebuilt directory + FTS" if not errs else "\n".join(errs))
    elif a.cmd == "reg":
        for e in h.reg.list(limit=a.limit):
            print(f"{e.rid:>6} {e.ts} {e.kind:<10} {e.actor:<9} {e.tool or '':<12} {e.status:<7} "
                  f"{('-> R:' + str(e.ref_id)) if e.ref_id else ''} {e.error or ''}")
    elif a.cmd == "replay":
        from .replay import replay
        rep = replay(h, a.session, a.librarian, a.approve_queued)
        print("\n".join(rep.log))
        print("\n--- diff (recorded -> replay) ---")
        print(rep.diff() or "(identical)")
    elif a.cmd == "research":
        from .agent import ResearchAgent
        from .librarian import run_librarian
        from .llm import LLM
        agent = ResearchAgent(h, LLM(h, a.llm, role="agent"),
                              max_searches=a.max_searches or h.cfg.get("agent_max_searches", 4),
                              budget_chars=h.cfg.get("agent_budget_chars", 40000),
                              on_event=lambda s: print("  ·", s))
        run = agent.run(a.entry)
        print()
        print(run.answer or "(no answer)")
        print()
        print(f"output R:{run.output_id}  retrievals {run.retrievals}  annotations {run.annotations}")
        print(run.timings())
        for rid in run.retrievals:
            if h.reg.annotation_for(rid):
                _, results = run_librarian(h, rid)
                for r in results:
                    print(f"  librarian {r.route:<8} {r.op.op} {getattr(r.op, 'id', '')}  {r.reason}")
        for line in run.log:
            if line.startswith(("warning", "gave up")):
                print(" ", line)
        pend = h.gateway.pending()
        if pend:
            print(f"  gate still locked by {', '.join(p.rid for p in pend)} — annotate (TUI ctrl+a) before the next search")
    elif a.cmd == "accept":
        r = h.accept_output(a.reg_id, a.room)
        print(f"{r.created[0]}  commit {r.commit[:8]}")
    elif a.cmd == "timings":
        runs = h.reg.list(kind="internal", tool="agent.finish", limit=a.last)
        print(f"{'run':>7} {'bounty':<7} {'turns':>5} {'model ms':>9} {'tool ms':>8}  model share")
        for r in runs:
            p = r.payload
            tot = max(1, p["model_ms"] + p["tool_ms"])
            print(f"R:{r.id:<5} {p['bounty']:<7} {p['turns']:>5} {p['model_ms']:>9} {p['tool_ms']:>8}  "
                  f"{100 * p['model_ms'] // tot}%")
    elif a.cmd == "render":
        if a.directory:
            print(h.paths.directory.read_text(encoding="utf-8"))
        elif a.plain:
            print(h.text(), end="")
        else:
            from rich.console import Console
            from .render import highlight_lobby
            from .theme import lobby_styles, palettes
            text, _ = highlight_lobby(h.text(), lobby_styles(palettes()[h.cfg.get("theme", "calculus-dark")]))
            Console(force_terminal=True, color_system="truecolor", width=200).print(text, end="")
    elif a.cmd == "snapshot":
        from .snapshot import snapshot
        print(snapshot(h, a.commands, a.keys, a.svg, a.size, a.wait))
    elif a.cmd == "backup":
        from .backup import backup
        info = backup(h.paths, Path(a.dest or h.cfg.get("backup_dir") or "~/hotel-backups"))
        print(json.dumps(info, indent=2))
    elif a.cmd == "forget":
        info = h.forget(a.reg_id, a.reason)
        print(f"forgot R:{a.reg_id}: {info['deleted']} of {len(info['blobs'])} blobs deleted; rows and chain intact")
        print("note: lobby text and earlier backups are not touched — rewrite git history / prune backups if needed")
    elif a.cmd == "custodian":
        for r in h.custodian():
            print(f"{r.route:<8} {r.op.op} {getattr(r.op, 'id', getattr(r.op, 'room', ''))}  {r.reason}")
    elif a.cmd == "move":
        from .models import MoveOp
        r = h.apply([MoveOp(op="move", id=a.entry, room=a.room, reason=a.reason)])
        print(f"{a.entry} -> {r.created[0]} in {a.room}  commit {r.commit[:8]}")
    elif a.cmd == "guest":
        from .guest import Guest
        from .llm import LLM
        note = None
        if a.note:
            from .models import AppendOp
            note = h.apply([AppendOp(op="append", room=a.room or h.cfg.get("default_room", "inbox"),
                                     type="monologue", body=a.message, refs=[], tags=[])]).created[0]
        g = Guest(h, LLM(h, a.llm, role="agent"), on_event=lambda s: print("  ·", s))
        t = g.say(a.message, note)
        print()
        print(t.reply or "(no reply) " + "; ".join(t.log[-2:]))
        if t.proposals:
            print("drafts in review: " + " ".join(f"R:{p}" for p in t.proposals) + "  (hotel review approve <id>)")
    elif a.cmd == "rag-import" or (a.cmd == "rag" and a.action == "import"):
        files = [Path(a.file)] if a.cmd == "rag-import" else [Path(x) for x in a.args]
        if not files:
            sys.exit("hotel rag import <file or directory>...")
        res = h.rag_import(files, a.title, progress=lambda f, did, new: print(
            f"RAG:{did:<5} {'new ' if new else 'same'} {len(h.rag.chunks(did)):>4} chunks  {f}"))
        if h.index is not None:
            print(f"embedded with {h.index.name}" if not h.index.error else f"embeddings unavailable: {h.index.error}")
        if not res:
            print("nothing importable (md, markdown, txt, rst, org, html, htm, pdf)")
    elif a.cmd == "rag" and a.action == "list":
        for d in h.rag.docs():
            state = f"forgotten {d['forgotten_at']}" if d["forgotten_at"] else f"{d['n_chunks']:>4} chunks {d['chars']:>8} chars"
            print(f"RAG:{d['id']:<5} {d['kind']:<9} {state}  {d['title'] or ''}")
    elif a.cmd == "rag" and a.action == "forget":
        if len(a.args) != 1 or not a.reason:
            sys.exit("hotel rag forget <id> --reason <why>")
        info = h.forget_rag(int(a.args[0].removeprefix("RAG:")), a.reason)
        print(f"forgot RAG:{a.args[0].removeprefix('RAG:')}: text blob {'deleted' if info['deleted'] else 'already gone'}, "
              f"{info['chunks']} chunks unindexed; the id stays so references still resolve")
    elif a.cmd == "labels":
        from .concierge import labels
        if a.export:
            info = labels.export(h)
        else:
            lab = labels.mine(h)
            info = {"reg_head": lab.reg_head, "counts": lab.counts(), "stats": labels.retrieval_stats(lab)}
        print(json.dumps(info, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
