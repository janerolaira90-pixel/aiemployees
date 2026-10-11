"""Claude client wrapper.

Returns plain dicts so responses can be stored verbatim in SQLite and replayed unchanged
(thinking blocks included) on the next request. Conversation history is append-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

# USD per million tokens (Claude API first-party rates).
PRICING = {
    "claude-opus-5-5": {"in": 4.00, "out": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-fable-5-1": {"in": 10.00, "out": 50.00, "cache_read": 0.25, "cache_write": 12.50},
}


@dataclass
class LLMResponse:
    content: list[dict]
    stop_reason: str | None
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    request_id: str | None = None
    stop_details: dict | None = None

    def cost_usd(self) -> float:
        p = PRICING.get(self.model) or PRICING["claude-opus-5-5"]
        u = self.usage
        return (
            u.get("input_tokens", 0) * p["in"]
            + u.get("output_tokens", 0) * p["out"]
            + u.get("cache_read_input_tokens", 0) * p["cache_read"]
            + u.get("cache_creation_input_tokens", 0) * p["cache_write"]
        ) / 1_000_000


class PermanentLLMError(RuntimeError):
    """A request the API will never accept as sent (bad request, auth, permission). Not retried."""


class LLM(Protocol):
    model: str

    def create(self, *, system: str, messages: list[dict], tools: list[dict], effort: str,
               agent_id: str) -> LLMResponse: ...


class ClaudeLLM:
    """Messages API with adaptive thinking, explicit effort, prompt caching, and refusal fallback."""

    def __init__(self, api_key: str | None, model: str = "claude-opus-5-5", max_tokens: int = 16000):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3)
        self.model = model
        self.max_tokens = max_tokens

    def create(self, *, system: str, messages: list[dict], tools: list[dict], effort: str,
               agent_id: str) -> LLMResponse:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},
        )
        if self.model in ("claude-opus-5-5", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5-5"):
            # Server-side fallback: a classifier false positive on benign marketing work
            # retries on the recommended model instead of failing the task.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        a = self._anthropic
        try:
            if "fallbacks" in kwargs:
                resp = self.client.beta.messages.create(**kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except (a.BadRequestError, a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError) as exc:
            raise PermanentLLMError(f"{type(exc).__name__}: {exc.message}") from exc
        # RateLimitError / 5xx / connection errors propagate and the job queue retries with backoff.
        usage = resp.usage.model_dump() if resp.usage else {}
        usage = {k: v for k, v in usage.items() if isinstance(v, int)}
        stop_details = resp.stop_details.model_dump() if getattr(resp, "stop_details", None) else None
        return LLMResponse(
            content=[b.model_dump(mode="json", exclude_none=True) for b in resp.content],
            stop_reason=resp.stop_reason,
            usage=usage,
            model=resp.model or self.model,
            request_id=getattr(resp, "_request_id", None),
            stop_details=stop_details,
        )


class UnconfiguredLLM:
    """Used when ANTHROPIC_API_KEY is missing: fails loudly and honestly."""

    model = "unconfigured"

    def create(self, **_: Any) -> LLMResponse:
        raise PermanentLLMError("ANTHROPIC_API_KEY is not set — the AI employees cannot think until it is configured.")
