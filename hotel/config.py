"""Filesystem layout of a hotel home directory.

<root>/
  lobby.txt              canonical lobby (git-tracked, append-only grammar)
  lobby.directory.txt    regenerated directory layer (derived, gitignored)
  .hotel/reg.sqlite      Reg: immutable append-only log + review queue
  .hotel/blobs/          content-addressed raw results (sha256)
  .hotel/derived.sqlite  FTS index over the lobby (derived, rebuildable)
  .hotel/rag.sqlite      imported / promoted knowledge
  .hotel/config.json     local settings
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def lobby(self) -> Path:
        return self.root / "lobby.txt"

    @property
    def directory(self) -> Path:
        return self.root / "lobby.directory.txt"

    @property
    def state(self) -> Path:
        return self.root / ".hotel"

    @property
    def reg_db(self) -> Path:
        return self.state / "reg.sqlite"

    @property
    def replay_db(self) -> Path:
        return self.state / "replay.sqlite"

    @property
    def blobs(self) -> Path:
        return self.state / "blobs"

    @property
    def derived_db(self) -> Path:
        return self.state / "derived.sqlite"

    @property
    def rag_db(self) -> Path:
        return self.state / "rag.sqlite"

    @property
    def config(self) -> Path:
        return self.state / "config.json"


DEFAULT_CONFIG = {
    "librarian": "rule",  # "rule" | "mercury"
    "mercury_model": "mercury-2.5",
    "mercury_base_url": "https://api.inceptionlabs.ai/v1",
    # Must be set true only AFTER you have disabled the training opt-in in the
    # Inception dashboard. The code cannot do that for you; it only refuses to
    # send lobby content until you assert it.
    "inception_training_opt_out_confirmed": False,
    "resident_rate_cap_per_hour": 30,
    "exa_num_results": 5,
    "exa_max_chars": 4000,
    "default_room": "inbox",
    "theme": "calculus-dark",
}


def find_root(start: Path | None = None) -> Path | None:
    env = os.environ.get("HOTEL_HOME")
    if env:
        return Path(env).expanduser().resolve()
    p = (start or Path.cwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / ".hotel").is_dir():
            return cand
    return None


def load_config(paths: Paths) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if paths.config.exists():
        cfg.update(json.loads(paths.config.read_text(encoding="utf-8")))
    return cfg


def save_config(paths: Paths, cfg: dict) -> None:
    paths.config.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
