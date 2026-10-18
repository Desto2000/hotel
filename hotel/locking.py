"""Cross-process exclusive lock (stdlib only; msvcrt on Windows, fcntl elsewhere).

The Reg is safe on its own (SQLite BEGIN IMMEDIATE). The lobby is a plain file
edited read-modify-write, so two writers (TUI + CLI, or a resident) could both
compute the same next id and the second write would drop the first. Every
lobby write holds this lock for the whole read -> validate -> write -> commit.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(RuntimeError):
    pass


@contextmanager
def file_lock(path: Path, timeout: float = 15.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise LockTimeout(f"could not lock {path} within {timeout}s (another hotel process is writing)")
                time.sleep(0.05)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
