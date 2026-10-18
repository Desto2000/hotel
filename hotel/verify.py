"""Entailment check for model-written annotations.

The gate proves a quote EXISTS in the stored source. It does not prove the
claim FOLLOWS from it. When `verifier` names an llm profile, each model claim
is judged against its own quote (and only that) before the annotation is
accepted; overreaching claims go back to the agent with the reason.

Use a different/stronger model than the agent where possible — a model
grading itself shares its blind spots. The verifier's calls are ordinary
`model` rows in the Reg (actor=verifier).
"""

from __future__ import annotations

import json
import re

from .llm import LLM

SYSTEM = """You check whether claims are entailed by quotes. For each item, judge ONLY from the quote text — no outside knowledge, no charity.
- "entailed": the quote states the claim (paraphrase is fine).
- "overreach": the quote supports a narrower or weaker statement than the claim (e.g. one benchmark row -> "the best").
- "unsupported": the quote does not state it, or says something else.
Return one JSON object: {"verdicts": [{"i": <item number>, "verdict": "entailed"|"overreach"|"unsupported", "why": "<12 words max>"}]}"""


def verify_claims(hotel, claims: list[dict], profile: str, gateway=None, transport=None) -> list[dict]:
    """claims: [{"text", "quote"}] -> [{"i", "verdict", "why"}] (same order)."""
    if not claims:
        return []
    llm = LLM(hotel, profile, role="verifier", gateway=gateway, transport=transport)
    items = "\n\n".join(f"Item {i}\nQuote: {c['quote']}\nClaim: {c['text']}" for i, c in enumerate(claims, 1))
    _, msg = llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": items}],
                      max_tokens=1024, actor="verifier")
    content = re.sub(r"^```(?:json)?|```$", "", (msg.get("content") or "").strip(), flags=re.M).strip()
    try:
        got = {int(v["i"]): v for v in json.loads(content)["verdicts"]}
    except Exception:
        # unparseable verdict = no verification; fail closed
        return [{"i": i, "verdict": "unsupported", "why": "verifier output unreadable"} for i in range(1, len(claims) + 1)]
    return [got.get(i, {"i": i, "verdict": "unsupported", "why": "no verdict returned"})
            for i in range(1, len(claims) + 1)]
