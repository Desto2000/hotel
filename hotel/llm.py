"""Thin router over OpenAI-compatible chat endpoints (no SDK, no framework).

Every call goes through the gateway as a `model` row: the request goes to a blob
(it can be large), the raw response is stored by hash, and the latency is in
the payload. Inference is logged but not gated; the gate applies to tool calls.

Profiles live in config["llm_profiles"]. Mercury is the default; any other
OpenAI-compatible endpoint works the same way.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .gateway import ToolResult, ToolSpec

DEFAULT_PROFILES = {
    "mercury": {
        "base_url": "https://api.inceptionlabs.ai/v1",
        "model": "mercury-2.5",
        "key_env": "INCEPTION_API_KEY",
        # Inception's own vocabulary: instant | low | medium | high
        "effort": {"agent": "low", "librarian": "low"},
        "require_training_optout": True,
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
        "key_env_fallback": "HERMES_CUSTOM_GROQ_API_KEY",
        "effort": {"agent": "low", "librarian": "low"},
        "require_training_optout": False,
    },
}


class LLMError(RuntimeError):
    pass


def _check(r: httpx.Response) -> httpx.Response:
    """raise_for_status, but keep the server's explanation (cookbook pattern)."""
    if r.is_error:
        raise LLMError(f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}")
    return r


@dataclass
class Profile:
    name: str
    base_url: str
    model: str
    key_env: str
    effort: dict
    require_training_optout: bool
    key_env_fallback: str | None = None

    def key(self) -> str:
        k = os.environ.get(self.key_env) or (os.environ.get(self.key_env_fallback) if self.key_env_fallback else None)
        if not k:
            raise LLMError(f"{self.key_env} not set (profile {self.name})")
        return k


def get_profile(cfg: dict, name: str | None = None) -> Profile:
    profiles = {**DEFAULT_PROFILES, **cfg.get("llm_profiles", {})}
    name = name or cfg.get("llm", "mercury")
    if name not in profiles:
        raise LLMError(f"unknown llm profile {name!r}; known: {', '.join(profiles)}")
    p = profiles[name]
    return Profile(name=name, base_url=p["base_url"], model=p["model"], key_env=p["key_env"],
                   effort=p.get("effort", {}), require_training_optout=p.get("require_training_optout", False),
                   key_env_fallback=p.get("key_env_fallback"))


def http_chat(profile: Profile, cfg: dict, body: dict) -> bytes:
    if profile.require_training_optout and not (
        cfg.get("inception_training_opt_out_confirmed")
        or os.environ.get("HOTEL_INCEPTION_TRAINING_OPTOUT") == "confirmed"
    ):
        raise LLMError(
            "refusing to send lobby content: turn off the API training opt-in in the provider dashboard, "
            "then set inception_training_opt_out_confirmed=true in .hotel/config.json"
        )
    headers = {"Authorization": f"Bearer {profile.key()}", "Content-Type": "application/json"}
    for attempt in range(5):
        r = httpx.post(f"{profile.base_url}/chat/completions", json=body, headers=headers, timeout=120)
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 4:
            # honour the provider's own hint (TPM windows) before blind backoff
            try:
                wait = float(r.headers.get("retry-after", ""))
            except ValueError:
                wait = 2 ** attempt
            time.sleep(min(max(wait, 1.0), 30.0))
            continue
        return _check(r).content
    raise LLMError("unreachable")


class LLM:
    """chat() -> assistant message dict, logged through the gateway."""

    def __init__(self, hotel, profile: str | None = None, role: str = "agent", gateway=None,
                 transport: Callable[[dict], bytes] | None = None):
        self.hotel = hotel
        self.profile = get_profile(hotel.cfg, profile)
        self.role = role
        self.gateway = gateway or hotel.gateway
        self.transport = transport or (lambda body: http_chat(self.profile, hotel.cfg, body))
        self.tool_name = f"llm.{self.profile.name}"
        if self.tool_name not in self.gateway.tools:
            self.gateway.register(ToolSpec(self.tool_name, "model", self.profile.name, self._call))

    @property
    def name(self) -> str:
        return f"{self.profile.name}:{self.profile.model}"

    def _call(self, body: dict) -> ToolResult:
        raw = self.transport(body)
        msg = json.loads(raw)["choices"][0]["message"]
        return ToolResult(raw, msg.get("content") or "", msg, self.profile.name)

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None, tool_choice: str | None = None,
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
