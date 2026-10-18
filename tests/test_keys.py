"""API key store + onboarding wizard (no network, no real ~/.config)."""

import httpx
import pytest

from hotel import keys
from hotel.config import Paths, load_config
from hotel.core import init_home
from hotel.llm import LLMError, get_profile

ENVS = ["EXA_API_KEY", "MCP_EXA_API_KEY", "INCEPTION_API_KEY", "PARALLEL_API_KEY", "GROQ_API_KEY",
        "HERMES_CUSTOM_GROQ_API_KEY"]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "cfg" / "keys.json")
    for e in ENVS:
        monkeypatch.delenv(e, raising=False)


def resp(status):
    return lambda *a, **k: httpx.Response(status, request=httpx.Request("GET", "http://x"))


def test_env_beats_file_and_alias_resolves(monkeypatch):
    keys.set_key("EXA_API_KEY", "from-file")
    assert keys.get("EXA_API_KEY") == "from-file" and keys.source("EXA_API_KEY") == "file"
    monkeypatch.setenv("MCP_EXA_API_KEY", "from-alias")
    assert keys.get("EXA_API_KEY") == "from-alias" and keys.source("EXA_API_KEY") == "env"


def test_model_profile_reads_stored_key_and_error_points_to_setup():
    prof = get_profile({}, "mercury")
    with pytest.raises(LLMError, match="hotel setup"):
        prof.key()
    keys.set_key("INCEPTION_API_KEY", "sk-test")
    assert prof.key() == "sk-test"


def test_validate_only_auth_failure_is_bad_key():
    p = keys.BY_ENV["INCEPTION_API_KEY"]
    assert keys.validate(p, "k", post=resp(200))[0] is True
    assert keys.validate(p, "k", post=resp(401))[0] is False
    assert keys.validate(p, "k", post=resp(500))[0] is None  # provider trouble != wrong key

    def boom(*a, **k):
        raise httpx.ConnectError("offline")
    assert keys.validate(p, "k", post=boom)[0] is None
    assert keys.validate(keys.BY_ENV["PARALLEL_API_KEY"], "k")[0] is None  # no check endpoint


def script(answers):
    it = iter(answers)
    return lambda prompt: next(it)


def test_wizard_retries_rejected_key_saves_good_one_and_records_optout(tmp_path):
    paths = init_home(tmp_path / "home")
    statuses = iter([401, 200, 200])  # exa: bad, then good; mercury: good
    post = lambda *a, **k: httpx.Response(next(statuses), request=httpx.Request("GET", "http://x"))
    out = []
    ok = keys.onboard(home=paths, ask=script(["y"]), secret=script(["bad", "good-exa", "mercury-key", "", ""]),
                      out=out.append, post=post)
    assert ok
    assert keys.get("EXA_API_KEY") == "good-exa" and keys.get("INCEPTION_API_KEY") == "mercury-key"
    assert keys.get("PARALLEL_API_KEY") is None  # skipped with Enter
    assert load_config(paths)["inception_training_opt_out_confirmed"] is True
    assert not any("good-exa" in line or "mercury-key" in line for line in out)  # keys never echoed


def test_wizard_declined_optout_leaves_gate_closed_and_missing_reported(tmp_path):
    paths = init_home(tmp_path / "home")
    out = []
    ok = keys.onboard(home=paths, ask=script(["n"]), secret=script(["", "", "", ""]), out=out.append)
    assert not ok and "EXA_API_KEY" in out[-1]
    assert load_config(paths)["inception_training_opt_out_confirmed"] is False


def test_wizard_leaves_environment_keys_alone(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "e")
    keys.onboard(ask=script([]), secret=script(["", "", ""]), out=lambda s: None, only=["EXA_API_KEY"])
    assert keys.get("EXA_API_KEY") == "e" and not keys.KEYS_FILE.exists()
