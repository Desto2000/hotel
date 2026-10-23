"""Filesystem layout of a hotel home directory.

<root>/
  lobby.txt              canonical lobby (git-tracked, append-only grammar)
  lobby.directory.txt    regenerated directory layer (derived, gitignored)
  .hotel/reg.sqlite      Reg: immutable append-only log + review queue
  .hotel/blobs/          content-addressed raw results (sha256)
  .hotel/derived.sqlite  FTS index over the lobby (derived, rebuildable)
  .hotel/rag.sqlite      imported / promoted knowledge (doc metadata + chunk index; text in blobs)
  .hotel/vectors.sqlite  embedding cache keyed by text hash (derived, not backed up)
  .hotel/labels/         training/eval data exported from the Reg (`hotel labels --export`)
  .hotel/config.json     local settings
  .hotel/hotel.log       event timeline (JSON lines; `hotel log` follows it)
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
    def vectors_db(self) -> Path:
        return self.state / "vectors.sqlite"

    @property
    def labels(self) -> Path:
        return self.state / "labels"

    @property
    def config(self) -> Path:
        return self.state / "config.json"

    @property
    def log(self) -> Path:
        return self.state / "hotel.log"


DEFAULT_CONFIG = {
    "librarian": "rule",  # "rule" | "model" (default llm profile) | a profile name
    "llm": "mercury",  # default router profile for agent + model librarian (see llm.py)
    "llm_profiles": {},  # overrides / extra OpenAI-compatible endpoints
    "librarian_output_mode": "json_schema",  # or "json_object"
    "search_backend": "exa",  # "exa" | "parallel" for agent fan-out
    "agent_max_searches": 4,
    "bounty_revalidate_days": 90,  # closed bounties get `+ revalidate: <date>`; custodian flags them after
    "verifier": None,  # llm profile that checks model-written claims against their quotes (None = off)
    "agent_num_results": 3,  # per query; agent retrievals stay small (context is a budget)
    "agent_highlight_chars": 700,
    "agent_budget_chars": 40000,  # transcript size before annotated retrievals are compacted
    # Must be set true only AFTER you have disabled the training opt-in in the
    # Inception dashboard. The code cannot do that for you; it only refuses to
    # send lobby content until you assert it.
    "inception_training_opt_out_confirmed": False,
    "resident_rate_cap_per_hour": 30,
    "exa_num_results": 5,
    "exa_max_chars": 4000,
    "exa_agent_effort": "medium",  # minimal|low|medium|high|xhigh (fixed price per run) | auto|ultra (metered, capped below)
    "exa_agent_max_cost": 5,  # USD ceiling, only used by auto/ultra
    "exa_agent_timeout_s": 900,
    # Local embeddings for hybrid lobby/RAG search (ONNX, runs offline after a one-time download).
    # "none" = lexical only. HOTEL_EMBEDDER overrides.
    "embedder": "fastembed:google/embeddinggemma-300m",
    # Mercury Edit ghost text in the composer (needs INCEPTION_API_KEY and the training opt-out).
    "suggest": True,
    "suggest_context": 4,  # lobby/RAG snippets sent with each suggestion request
    # Reading view of search results: fold page chrome (menus, buttons, bylines) without touching the
    # stored text. "model" = a rule plus the default llm profile (line numbers only), "rule" = offline,
    # "off" = no chrome folding, or a profile name.
    "reading_filter": "model",
    # After filing an annotation, ask the default llm profile which other entries a claim is relevant
    # to; each suggestion is a claim ref waiting in Review. "off" disables it; a profile name picks one.
    "related_refs": "model",
    "default_room": "inbox",
    "theme": "calculus-dark",
}


USER_HOME_FILE = Path.home() / ".config" / "hotel" / "home"


def find_root(start: Path | None = None) -> Path | None:
    """HOTEL_HOME env -> nearest ancestor with .hotel/ -> per-user default
    (~/.config/hotel/home, written by `hotel init` / `hotel home`)."""
    env = os.environ.get("HOTEL_HOME")
    if env:
        return Path(env).expanduser().resolve()
    p = (start or Path.cwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / ".hotel").is_dir():
            return cand
    if USER_HOME_FILE.exists():
        d = Path(USER_HOME_FILE.read_text(encoding="utf-8").strip()).expanduser()
        if (d / ".hotel").is_dir():
            return d.resolve()
    return None


def set_default_home(root: Path) -> None:
    USER_HOME_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_HOME_FILE.write_text(str(root.resolve()) + "\n", encoding="utf-8")


def load_config(paths: Paths) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if paths.config.exists():
        cfg.update(json.loads(paths.config.read_text(encoding="utf-8")))
    return cfg


def save_config(paths: Paths, cfg: dict) -> None:
    paths.config.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
