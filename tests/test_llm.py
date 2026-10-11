"""The Claude wrapper builds the intended request and preserves response blocks verbatim."""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from eunaria.llm import ClaudeLLM, PermanentLLMError

RESPONSE = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "text", "text": "Delegating."},
        {"type": "tool_use", "id": "toolu_1", "name": "delegate_task",
         "input": {"agent": "chloe", "title": "Ideas", "instructions": "…"}},
    ],
    "stop_reason": "tool_use", "stop_sequence": None,
    "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 1000,
              "cache_creation_input_tokens": 0},
}


def _llm(handler) -> ClaudeLLM:
    llm = ClaudeLLM(api_key="test-key")
    llm.client = anthropic.Anthropic(api_key="test-key", max_retries=0,
                                     http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return llm


def test_request_shape_and_block_preservation():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(200, json=RESPONSE)

    resp = _llm(handler).create(system="sys", messages=[{"role": "user", "content": "hi"}],
                                tools=[{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
                                effort="high", agent_id="atlas")
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "budget_tokens" not in json.dumps(body) and "tool_choice" not in body

    assert [b["type"] for b in resp.content] == ["thinking", "text", "tool_use"]
    assert resp.content[0]["signature"] == "sig-abc"
    assert resp.stop_reason == "tool_use"
    # 1200*4 + 300*20 + 1000*0.2 per million
    assert resp.cost_usd() == pytest.approx((1200 * 4 + 300 * 20 + 1000 * 0.2) / 1e6)


def test_bad_requests_are_permanent_errors():
    def handler(request):
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                      "message": "bad"}})
    with pytest.raises(PermanentLLMError):
        _llm(handler).create(system="s", messages=[{"role": "user", "content": "x"}], tools=[], effort="low",
                             agent_id="ava")


def test_connection_errors_report_their_real_cause():
    def handler(request):
        raise httpx2.ConnectError("Name or service not known")
    with pytest.raises(ConnectionError) as info:
        _llm(handler).create(system="s", messages=[{"role": "user", "content": "x"}], tools=[], effort="low",
                             agent_id="ava")
    assert "Name or service not known" in str(info.value)


def test_pasted_secrets_are_cleaned(monkeypatch):
    from eunaria.config import Settings
    monkeypatch.setenv("ANTHROPIC_API_KEY", '  "sk-ant-test"\n')
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", " 5950299525 ")
    s = Settings.from_env(dotenv=None)
    assert s.anthropic_api_key == "sk-ant-test" and s.telegram_admin_ids == frozenset({5950299525})


def test_rejected_optional_features_fall_back_to_simpler_request():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "fallbacks" in body:
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                          "message": "fallbacks: not permitted"}})
        return httpx2.Response(200, json=RESPONSE)

    llm = _llm(handler)
    resp = llm.create(system="s", messages=[{"role": "user", "content": "x"}], tools=[], effort="high", agent_id="a")
    assert resp.stop_reason == "tool_use"
    assert "fallbacks" in bodies[0] and "fallbacks" not in bodies[1]
    assert bodies[1]["thinking"] == {"type": "adaptive"}       # keeps everything else
    llm.create(system="s", messages=[{"role": "user", "content": "x"}], tools=[], effort="high", agent_id="a")
    assert len(bodies) == 3 and "fallbacks" not in bodies[2]   # remembered; no repeat 400


def test_out_of_credit_is_reported_plainly_and_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API."}})
    with pytest.raises(PermanentLLMError) as info:
        _llm(handler).create(system="s", messages=[{"role": "user", "content": "x"}], tools=[], effort="low",
                             agent_id="ava")
    assert "out of credit" in str(info.value) and len(calls) == 1
