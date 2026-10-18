"""hotel CLI. `hotel` with no args opens the TUI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Paths, find_root


def _hotel(args, **kw):
    from .core import Hotel
    root = Path(args.home).resolve() if args.home else find_root()
    if root is None or not (root / ".hotel").is_dir():
        sys.exit("no hotel home found — run `hotel init <dir>` or set HOTEL_HOME")
    return Hotel(Paths(root), **kw)


def main(argv=None):
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
    s = sub.add_parser("rag-import"); s.add_argument("file"); s.add_argument("--title")
    sub.add_parser("sessions")
    s = sub.add_parser("research", help="run the research agent on a bounty"); s.add_argument("entry")
    s.add_argument("--llm", help="router profile (default: config llm)"); s.add_argument("--max-searches", type=int)
    s = sub.add_parser("accept", help="keep an agent output as an approved lobby entry"); s.add_argument("reg_id", type=int)
    s.add_argument("--room")
    s = sub.add_parser("timings", help="model vs tool latency per agent run"); s.add_argument("--last", type=int, default=5)
    s = sub.add_parser("render", help="print the lobby (highlighted, or --plain / --directory)")
    s.add_argument("--plain", action="store_true"); s.add_argument("--directory", action="store_true")
    s = sub.add_parser("snapshot", help="run the TUI headless with scripted commands; dump text (+ svg)")
    s.add_argument("--cmd", dest="commands", action="append", default=[]); s.add_argument("--svg"); s.add_argument("--keys", action="append", default=[])
    s.add_argument("--size", default="190x46"); s.add_argument("--wait", type=float, default=0.5)
    sub.add_parser("schema", help="print the librarian JSON schema")

    a = p.parse_args(argv)
    if a.cmd == "init":
        from .core import init_home
        paths = init_home(Path(a.dir))
        print(f"initialised hotel home at {paths.root}")
        return
    if a.cmd == "schema":
        from .models import LibrarianOutput
        print(json.dumps(LibrarianOutput.model_json_schema(), indent=2))
        return
    if a.cmd in (None, "tui"):
        from .tui import HotelApp
        h = _hotel(a, new_session=getattr(a, "new_session", False))
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
        runs = [r for r in h.reg.list(kind="internal", limit=5000) if r.tool == "agent.finish"][-a.last:]
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
    elif a.cmd == "rag-import":
        text = Path(a.file).read_text(encoding="utf-8")
        rid = h.rag.add("import", str(Path(a.file).resolve()), a.title or Path(a.file).name, text)
        h.gateway.log("internal", "user", tool="rag.import", payload={"rag_id": rid, "file": a.file})
        print(f"RAG:{rid}")


if __name__ == "__main__":
    main()
