"""API keys: where they live, how they are found, and the onboarding wizard.

Lookup order for any key: process environment (incl. the provider's legacy alias),
then the per-user key file (~/.config/hotel/keys.json, mode 0600 where the OS
supports it). Keys never go into the hotel home: that directory is backed up and
its lobby is git-tracked.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from exa_py import Exa

KEYS_FILE = Path.home() / ".config" / "hotel" / "keys.json"


@dataclass(frozen=True)
class Provider:
    env: str
    name: str
    purpose: str
    url: str  # where to get a key
    required: bool = False
    aliases: tuple[str, ...] = ()
    check: Callable[[str], int] | None = None  # cheap SDK call with the key -> HTTP status; raises OSError when unreachable


def _exa_status(key: str) -> int:
    try:
        Exa(api_key=key, timeout=15).search("hotel", num_results=1)
    except ValueError as ex:  # exa-py: "Request failed with status code 401: ..."
        m = re.search(r"status code (\d{3})", str(ex))
        if m:
            return int(m.group(1))
        raise
    return 200


def _models_status(sdk: str) -> Callable[[str], int]:
    def check(key: str) -> int:
        from .llm import SDKS  # llm imports keys; resolved lazily
        client_cls, status_error = SDKS[sdk]
        try:
            client_cls(api_key=key, timeout=15, max_retries=0).models.list()
        except status_error as ex:
            return ex.status_code
        except Exception as ex:  # the SDKs' own connection/timeout errors are not OSErrors
            if ex.__class__.__name__ in ("APIConnectionError", "APITimeoutError"):
                raise ConnectionError(str(ex)) from ex
            raise
        return 200
    return check


PROVIDERS = [
    Provider("EXA_API_KEY", "Exa", "web search for the research agent and /s, /exa, /fetch", "https://dashboard.exa.ai/api-keys",
             required=True, aliases=("MCP_EXA_API_KEY",),
             check=_exa_status),
    Provider("INCEPTION_API_KEY", "Mercury (Inception)", "default model for the agent, guest and model librarian",
             "https://platform.inceptionlabs.ai/", required=True,
             check=_models_status("inception")),
    Provider("PARALLEL_API_KEY", "Parallel", "optional alternative search backend", "https://platform.parallel.ai/"),
    Provider("GROQ_API_KEY", "Groq", "optional `groq` model profile", "https://console.groq.com/keys",
             aliases=("HERMES_CUSTOM_GROQ_API_KEY",),
             check=_models_status("groq")),
]
BY_ENV = {p.env: p for p in PROVIDERS}


def _read() -> dict[str, str]:
    try:
        d = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as ex:
        raise RuntimeError(f"cannot read {KEYS_FILE}: {ex}") from ex
    return {k: v for k, v in d.items() if isinstance(v, str) and v}


def _write(d: dict[str, str]) -> None:
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEYS_FILE.with_name(KEYS_FILE.name + ".tmp")
    tmp.unlink(missing_ok=True)  # O_CREAT's mode only applies to a new file
    # Created 0600 from the start, so the keys are never readable by others, not even briefly.
    # Windows ignores the mode and relies on the profile ACL.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, indent=2) + "\n")
    os.replace(tmp, KEYS_FILE)


def source(env: str) -> str | None:
    """Where the key for `env` comes from: "env", "file" or None."""
    names = (env, *(BY_ENV[env].aliases if env in BY_ENV else ()))
    if any(os.environ.get(n) for n in names):
        return "env"
    return "file" if _read().get(env) else None


def get(env: str, *aliases: str) -> str | None:
    """Environment (name, then aliases), then the key file. None when unset."""
    for n in (env, *aliases, *(BY_ENV[env].aliases if env in BY_ENV else ())):
        if os.environ.get(n):
            return os.environ[n]
    return _read().get(env)


def set_key(env: str, value: str) -> None:
    value = value.strip()
    if not value:
        raise ValueError("empty key")
    _write({**_read(), env: value})


def remove_key(env: str) -> bool:
    d = _read()
    if env not in d:
        return False
    del d[env]
    _write(d)
    return True


def mask(key: str) -> str:
    return key[:4] + "…" + key[-2:] if len(key) > 10 else "…"


def validate(p: Provider, key: str, *, probe: Callable[[str], int] | None = None) -> tuple[bool | None, str]:
    """(True|False|None, detail). None = cannot tell (no check, or network trouble);
    only a 401/403 counts as a bad key. `probe(key)` returns an HTTP status (tests inject it)."""
    check = probe or p.check
    if check is None:
        return None, "no cheap check available; key saved unverified"
    try:
        status = check(key)
    except OSError as ex:  # requests (Exa) and our own ConnectionError (OpenAI) both derive from OSError
        return None, f"could not reach {p.name}: {ex.__class__.__name__}"
    if status in (401, 403):
        return False, f"rejected ({status})"
    if status >= 400 and status != 429:
        return None, f"unexpected {status}; key may still be fine"
    return True, "accepted"


def status() -> list[dict]:
    return [{"env": p.env, "name": p.name, "required": p.required, "purpose": p.purpose,
             "source": source(p.env)} for p in PROVIDERS]


def missing_required() -> list[Provider]:
    return [p for p in PROVIDERS if p.required and source(p.env) is None]


def onboarded() -> bool:
    """True once setup ran (CLI wizard or the TUI's first-run screen)."""
    return KEYS_FILE.with_name("onboarded").exists()


def mark_onboarded() -> None:
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEYS_FILE.with_name("onboarded").touch()


def confirm_optout(home) -> None:
    """Record in the home config that the Mercury training opt-out is off in the dashboard."""
    from .config import load_config, save_config
    saved = json.loads(home.config.read_text(encoding="utf-8")) if home.config.exists() else dict(load_config(home))
    saved["inception_training_opt_out_confirmed"] = True
    save_config(home, saved)


# ------------------------------------------------------------------ wizard
def onboard(*, home=None, ask: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass,
            out: Callable[[str], None] = print, check: bool = True, only: list[str] | None = None,
            probe: Callable[[str], int] | None = None) -> bool:
    """Walk the user through each provider. Enter keeps the current state / skips.
    Returns True when every required key is available afterwards."""
    out("hotel setup — API keys\n")
    out(f"Keys are stored in {KEYS_FILE} (outside your lobby and its backups).")
    out("Environment variables always win over stored keys. Press Enter to skip a provider.\n")
    for p in PROVIDERS:
        if only and p.env not in only:
            continue
        src = source(p.env)
        tag = "required" if p.required else "optional"
        out(f"{p.name} [{tag}] — {p.purpose}")
        if src == "env":
            out(f"  {p.env} is set in your environment; nothing to do.\n")
            continue
        if src == "file":
            ans = ask(f"  stored key {mask(get(p.env) or '')} — replace it? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                out("")
                continue
        else:
            out(f"  get a key: {p.url}")
        for _ in range(3):
            key = secret(f"  paste {p.env} (hidden, Enter to skip): ").strip()
            if not key:
                out("  skipped\n")
                break
            if check:
                good, detail = validate(p, key, probe=probe)
                if good is False:
                    out(f"  {detail} — check the key and try again")
                    continue
                out(f"  {detail}")
            set_key(p.env, key)
            out(f"  saved {mask(key)}\n")
            break
    if home is not None and (only is None or "INCEPTION_API_KEY" in only):
        _optout_step(home, ask, out)
    mark_onboarded()
    left = missing_required()
    if left:
        out("still missing (required): " + ", ".join(p.env for p in left) + " — rerun `hotel setup` any time")
    else:
        out("all required keys available. `hotel doctor` checks the rest; `hotel` opens the lobby.")
    return not left


def _optout_step(home, ask, out) -> None:
    from .config import load_config
    cfg = load_config(home)
    if cfg.get("inception_training_opt_out_confirmed"):
        return
    out("Mercury trains on API traffic unless you opt out in the Inception dashboard.")
    out("hotel refuses to send lobby content to it until you confirm you have done that.")
    ans = ask("  have you turned the training opt-in OFF in the dashboard? [y/N] ").strip().lower()
    if ans in ("y", "yes"):
        confirm_optout(home)
        out("  recorded in .hotel/config.json\n")
    else:
        out("  not confirmed — model features stay blocked until you do (rerun `hotel setup`)\n")


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
