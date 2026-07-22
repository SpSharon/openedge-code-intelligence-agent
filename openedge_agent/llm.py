"""Pluggable LLM client for the Stage 2 agent (same pattern as the
ChurchReach template's llm.py: one small interface, a real Anthropic
backend, a scripted FakeLLM for offline tests, and a usage meter that
gets stamped into every scoreboard so no number is ever quoted without
its provenance).

The interface is a single method:

    complete(system: str, user: str, max_tokens: int = 1024) -> str

Backends:
    AnthropicLLM   real API. Needs ANTHROPIC_API_KEY in the environment
                   (never a committed file). Model comes from the
                   OE_AGENT_MODEL env var or the constructor.
    FakeLLM        scripted responses (a list, or a callable taking
                   (system, user)). Fully offline; records every prompt
                   it saw so tests can assert on grounding.

Both share a Meter: calls, input/output tokens (real token counts from
the API when available, a chars/4 estimate for FakeLLM), and an
estimated cost derived from a small static price table. The price table
is for pre-spend ESTIMATES only — treat the printed dollar figure as an
estimate, not a bill.
"""

from __future__ import annotations

import os

DEFAULT_MODEL = os.environ.get("OE_AGENT_MODEL", "claude-sonnet-4-5")

# USD per million tokens (input, output) by model-name prefix. Static and
# possibly stale — used only for the printed pre-spend estimate.
PRICES_PER_MTOK = {
    "claude-opus": (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
}
_DEFAULT_PRICE = (3.0, 15.0)


def price_for(model: str) -> tuple[float, float]:
    for prefix, price in sorted(PRICES_PER_MTOK.items(), key=lambda kv: -len(kv[0])):
        if model.startswith(prefix):
            return price
    return _DEFAULT_PRICE


def approx_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token for code-heavy English)."""
    return max(1, len(text) // 4)


class Meter:
    """Accumulates usage across calls; serializable into scoreboards."""

    def __init__(self, model: str):
        self.model = model
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.tokens_estimated = False  # True if any counts were estimates

    def add(self, input_tokens: int, output_tokens: int, estimated: bool = False):
        self.calls += 1
        self.input_tokens += int(input_tokens)
        self.output_tokens += int(output_tokens)
        if estimated:
            self.tokens_estimated = True

    @property
    def estimated_cost_usd(self) -> float:
        pi, po = price_for(self.model)
        return (self.input_tokens * pi + self.output_tokens * po) / 1_000_000

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens_estimated": self.tokens_estimated,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
        }


class FakeLLM:
    """Offline scripted backend.

    responses: either a list of strings (returned in order, last one
    repeating) or a callable (system, user) -> str.
    """

    def __init__(self, responses=None):
        if responses is None:
            responses = ["CITATIONS: none"]
        self._responses = responses
        self._i = 0
        self.prompts: list[tuple[str, str]] = []  # (system, user) per call
        self.meter = Meter(model="fake-llm")

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        self.prompts.append((system, user))
        if callable(self._responses):
            out = self._responses(system, user)
        else:
            out = self._responses[min(self._i, len(self._responses) - 1)]
            self._i += 1
        self.meter.add(
            approx_tokens(system) + approx_tokens(user),
            approx_tokens(out),
            estimated=True,
        )
        return out


class AnthropicLLM:
    """Real Anthropic backend. Import + key check happen lazily so the
    rest of the package stays importable offline."""

    def __init__(self, model: str | None = None, temperature: float = 0.0):
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.meter = Meter(model=self.model)
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Export it as a session "
                    "env var (never commit it) or use FakeLLM offline."
                )
            import anthropic  # deferred: not installed in offline envs

            self._client = anthropic.Anthropic()
        return self._client

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        client = self._ensure_client()
        resp = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=self.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.meter.add(usage.input_tokens, usage.output_tokens)
        else:  # pragma: no cover - defensive
            self.meter.add(approx_tokens(system + user), 0, estimated=True)
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
