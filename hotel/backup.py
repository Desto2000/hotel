"""Backup / restore of a hotel home.

Layout of a backup root (incremental, add-only):

  <dest>/blobs/ab/cdef…          content-addressed; only missing blobs are copied
  <dest>/snapshots/<ts>/
      reg.sqlite  rag.sqlite     consistent copies via SQLite's online backup API
      lobby.bundle               `git bundle --all` of the lobby repo (full history)
      config.json  MANIFEST.json counts, hashes, reg chain head

Derived state (FTS index, directory file) is not backed up: it is rebuilt.
Restore verifies the Reg hash chain and that every blob the Reg references
is present (or explicitly redacted) before declaring success.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import Paths


def _sqlite_copy(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dst))
    with d:
        s.backup(d)
    s.close()
    d.close()


def referenced_blobs(reg_db: Path, rag_db: Path | None = None) -> set[str]:
    """Blobs the Reg (and, given rag_db, the RAG store's live documents) point at."""
    c = sqlite3.connect(str(reg_db))
    out: set[str] = set()
    for rh, th, payload in c.execute("SELECT result_hash, text_hash, payload FROM reg"):
        out.update(h for h in (rh, th) if h)
        if payload:
            p = json.loads(payload)
            for k in ("args_blob", "annotation_blob", "before", "after"):
                if isinstance(p.get(k), str) and len(p[k]) == 64:
                    out.add(p[k])
    for (snap,) in c.execute("SELECT lobby_snapshot FROM sessions"):
        if snap:
            out.add(snap)
    redacted = set()
    for (payload,) in c.execute("SELECT payload FROM reg WHERE kind='redaction'"):
        redacted.update(json.loads(payload).get("blobs", []))
    c.close()
    if rag_db is not None and rag_db.exists():
        r = sqlite3.connect(str(rag_db))
        try:
            out.update(h for (h,) in r.execute("SELECT blob FROM docs WHERE blob IS NOT NULL"))
        except sqlite3.OperationalError:  # v1 store: text was inline
            pass
        r.close()
    return out - redacted


def backup(paths: Paths, dest: Path) -> dict:
    from .core import git
    dest = dest.expanduser().resolve()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = dest / "snapshots" / ts
    n = 1
    while snap.exists():  # two backups in the same second
        n += 1
        snap = dest / "snapshots" / f"{ts}-{n}"
    snap.mkdir(parents=True)
    _sqlite_copy(paths.reg_db, snap / "reg.sqlite")
    _sqlite_copy(paths.rag_db, snap / "rag.sqlite")
    if paths.config.exists():
        shutil.copy2(paths.config, snap / "config.json")
    r = git(paths.root, "bundle", "create", str(snap / "lobby.bundle"), "--all")
    if r.returncode != 0:
        raise RuntimeError(f"git bundle failed: {r.stderr.strip()}")
    copied = present = 0
    for h in referenced_blobs(snap / "reg.sqlite", snap / "rag.sqlite"):
        src = paths.blobs / h[:2] / h[2:]
        dst = dest / "blobs" / h[:2] / h[2:]
        if dst.exists():
            present += 1
            continue
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    c = sqlite3.connect(str(snap / "reg.sqlite"))
    rows, head = c.execute("SELECT COUNT(*), (SELECT entry_hash FROM reg ORDER BY id DESC LIMIT 1) FROM reg").fetchone()
    c.close()
    manifest = {"created": ts, "source": str(paths.root), "reg_rows": rows, "reg_head": head,
                "blobs_copied": copied, "blobs_already_present": present,
                "lobby_head": git(paths.root, "rev-parse", "HEAD").stdout.strip()}
    (snap / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"snapshot": str(snap), **manifest}


def latest_snapshot(src: Path) -> Path:
    def key(p: Path):
        stamp, _, n = p.parent.name.partition("-")
        return stamp, int(n or 1)
    snaps = sorted((src / "snapshots").glob("*/MANIFEST.json"), key=key)
    if not snaps:
        raise FileNotFoundError(f"no snapshots under {src}")
    return snaps[-1].parent


def restore(src: Path, target: Path, snapshot: str | None = None) -> dict:
    """Restore into an empty/non-existent directory, then verify."""
    from .core import Hotel, git
    src = src.expanduser().resolve()
    target = target.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} is not empty")
    snap = src / "snapshots" / snapshot if snapshot else latest_snapshot(src)
    r = git(target.parent, "clone", "-q", str(snap / "lobby.bundle"), str(target))
    if r.returncode != 0:
        raise RuntimeError(f"git clone from bundle failed: {r.stderr.strip()}")
    paths = Paths(target)
    paths.state.mkdir(parents=True, exist_ok=True)
    for name in ("reg.sqlite", "rag.sqlite", "config.json"):
        if (snap / name).exists():
            shutil.copy2(snap / name, paths.state / name)
    missing = []
    for h in referenced_blobs(paths.reg_db, paths.rag_db):
        s = src / "blobs" / h[:2] / h[2:]
        if not s.exists():
            missing.append(h)
            continue
        d = paths.blobs / h[:2] / h[2:]
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    h = Hotel(paths)  # rebuilds derived layers
    problems = h.reg.verify_chain() + [f"missing blob {m}" for m in missing] + h.check()
    return {"restored": str(target), "snapshot": snap.name, "problems": problems}
