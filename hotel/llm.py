"""Thin router over chat endpoints through each provider's own SDK (no framework).

Every call goes through the gateway as a `model` row: the request goes to a blob
(it can be large), the raw response is stored by hash, and the latency is in
the payload. Inference is logged but not gated; the gate applies to tool calls.

Profiles live in config["llm_profiles"]. Mercury is the default (`inceptionai`
SDK); Groq uses `groq`; any other OpenAI-compatible endpoint takes sdk="openai"
plus a base_url.
"""

from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from typing import Callable

from . import keys
from .gateway import ToolResult, ToolSpec

DEFAULT_PROFILES = {
    "mercury": {
        "sdk": "inception",
        "model": "mercury-3",
        "key_env": "INCEPTION_API_KEY",
        # Inception's own vocabulary: instant | low | medium | high
        "effort": {"agent": "low", "librarian": "low", "reader": "instant"},
        "require_training_optout": True,
    },
    "groq": {
        "sdk": "groq",
        "model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
        "key_env_fallback": "HERMES_CUSTOM_GROQ_API_KEY",
        "effort": {"agent": "low", "librarian": "low", "reader": "low"},
        "require_training_optout": False,
    },
}


class LLMError(RuntimeError):
    pass


# sdk name -> (module, client class); keys.py probes keys through the same table
SDKS = {"inception": ("inceptionai", "Inception"), "groq": ("groq", "Groq"), "openai": ("openai", "OpenAI")}


def sdk(name: str) -> tuple[type, type[Exception]]:
    """(client class, its APIStatusError). Imported on first use: the SDKs cost ~0.4 s at startup."""
    mod = importlib.import_module(SDKS[name][0])
    return getattr(mod, SDKS[name][1]), mod.APIStatusError


@dataclass
class Profile:
    name: str
    sdk: str
    model: str
    key_env: str
    effort: dict
    require_training_optout: bool
    key_env_fallback: str | None = None
    base_url: str | None = None  # None = the SDK's own default host
    price: dict | None = None  # {"in": usd_per_mtok, "out": usd_per_mtok}; set in config to log cost

    def key(self) -> str:
        k = keys.get(self.key_env, *([self.key_env_fallback] if self.key_env_fallback else []))
        if not k:
            raise LLMError(f"{self.key_env} not set (profile {self.name}) — run `hotel setup`")
        return k


def get_profile(cfg: dict, name: str | None = None) -> Profile:
    profiles = {**DEFAULT_PROFILES, **cfg.get("llm_profiles", {})}
    name = name or cfg.get("llm", "mercury")
    if name not in profiles:
        raise LLMError(f"unknown llm profile {name!r}; known: {', '.join(profiles)}")
    p = {**DEFAULT_PROFILES.get(name, {}), **cfg.get("llm_profiles", {}).get(name, {})}  # partial overrides ok
    return Profile(name=name, sdk=p.get("sdk", "openai"), base_url=p.get("base_url"), model=p["model"], key_env=p["key_env"],
                   effort=p.get("effort", {}), require_training_optout=p.get("require_training_optout", False),
                   key_env_fallback=p.get("key_env_fallback"), price=p.get("price"))


def _optout_ok(profile: Profile, cfg: dict) -> bool:
    return not profile.require_training_optout or bool(
        cfg.get("inception_training_opt_out_confirmed")
        or os.environ.get("HOTEL_INCEPTION_TRAINING_OPTOUT") == "confirmed")


def unavailable(cfg: dict, name: str | None = None) -> str | None:
    """Why a profile cannot be called right now (no key, training opt-out not confirmed), or None.
    Optional model features check this first and quietly fall back instead of logging a failure."""
    try:
        p = get_profile(cfg, name)
        p.key()
    except LLMError as ex:
        return str(ex)
    return None if _optout_ok(p, cfg) else "training opt-out not confirmed (/setup)"


def http_chat(profile: Profile, cfg: dict, body: dict) -> bytes:
    if not _optout_ok(profile, cfg):
        raise LLMError(
            "refusing to send lobby content: turn off the API training opt-in in the provider dashboard, "
            "then set inception_training_opt_out_confirmed=true in .hotel/config.json"
        )
    # the SDKs back off on 429/5xx and honours the provider's retry-after hint
    client_cls, status_error = sdk(profile.sdk)
    client = client_cls(api_key=profile.key(), base_url=profile.base_url, timeout=120, max_retries=4)
    try:
        raw = client.chat.completions.with_raw_response.create(**body)
    except status_error as ex:  # keep the server's explanation (cookbook pattern)
        raise LLMError(f"POST {ex.request.url} -> {ex.status_code}: {ex.response.text[:400]}") from ex
    return raw.http_response.content  # the exact bytes: they are stored by hash


class LLM:
    """chat() -> assistant message dict, logged through the gateway."""

    def __init__(self, hotel, profile: str | None = None, role: str = "agent", gateway=None,
                 transport: Callable[[dict], bytes] | None = None):
        self.hotel = hotel
        self.profile = get_profile(hotel.cfg, profile)
        self.role = role
        self.gateway = gateway or hotel.gateway
        self.transport = transport or (lambda body: http_chat(self.profile, hotel.cfg, body))
        self.tool_name = f"llm.{self.profile.name}.{role}"
        # (re)register: the newest instance for a profile+role owns the tool, so
        # an agent and its verifier on the same profile never share a transport
        self.gateway.register(ToolSpec(self.tool_name, "model", self.profile.name, self._call))

    @property
    def name(self) -> str:
        return f"{self.profile.name}:{self.profile.model}"

    def _call(self, body: dict) -> ToolResult:
        raw = self.transport(body)
        resp = json.loads(raw)
        msg = resp["choices"][0]["message"]
        u = resp.get("usage") or {}
        meta = {"model": self.profile.model,
                "tokens_in": u.get("prompt_tokens"), "tokens_out": u.get("completion_tokens")}
        price = self.profile.price  # USD per 1M tokens, only if configured
        if price and meta["tokens_in"] is not None and meta["tokens_out"] is not None:
            meta["cost_usd"] = round(meta["tokens_in"] * price["in"] / 1e6 + meta["tokens_out"] * price["out"] / 1e6, 6)
        return ToolResult(raw, msg.get("content") or "", msg, self.profile.name, meta)

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None, tool_choice: str | dict | None = None,
             json_schema: dict | None = None, max_tokens: int = 4096, actor: str | None = None):
        body: dict = {"model": self.profile.model, "messages": messages, "max_completion_tokens": max_tokens}
        effort = self.profile.effort.get(self.role)
        if effort:
            body["reasoning_effort"] = effort
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        if json_schema:
            body["response_format"] = {"type": "json_schema", "json_schema": json_schema}
        entry, res = self.gateway.call(self.tool_name, actor=actor or self.role, body=body)
        return entry, res.data


class ScriptedTransport:
    """Deterministic stand-in for a model (tests / offline experiments).
    Each step is a function(body) -> assistant message dict."""

    def __init__(self, steps: list[Callable[[dict], dict]]):
        self.steps = list(steps)
        self.bodies: list[dict] = []

    def __call__(self, body: dict) -> bytes:
        self.bodies.append(body)
        if not self.steps:
            msg = {"role": "assistant", "content": "(script exhausted)"}
        else:
            msg = self.steps.pop(0)(body)
        msg.setdefault("role", "assistant")
        return json.dumps({"choices": [{"message": msg}]}).encode()
