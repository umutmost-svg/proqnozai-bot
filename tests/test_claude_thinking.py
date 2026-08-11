"""Adaptive thinking on the forecast call, and its fallback behaviour.

Opus 4.7+ rejects the legacy {"type":"enabled","budget_tokens":N} form with a
400 ("thinking.type.enabled is not supported for this model"), so the request
must use adaptive thinking + output_config.effort. These tests pin the request
shape and the three failure paths. Offline: the SDK call is stubbed.
"""
import httpx
import anthropic
import pytest

import claude_client as cc


class _Block:
    def __init__(self, text):
        self.type = "text"; self.text = text


class _Resp:
    def __init__(self, text="forecast text"):
        self.content = [_Block(text)]


def _bad_request(msg='"thinking.type.enabled" is not supported for this model'):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, request=request)
    return anthropic.BadRequestError(msg, response=response, body=None)


@pytest.fixture
def calls(monkeypatch, temp_db):
    """Record every messages.create kwarg; `calls.results` drives what each
    successive call returns (an Exception instance is raised)."""
    recorded = []

    class _Recorder(list):
        results = [_Resp()]

    recorded = _Recorder()

    def _fake_create(**kwargs):
        recorded.append(kwargs)
        idx = min(len(recorded) - 1, len(recorded.results) - 1)
        out = recorded.results[idx]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(cc.client.messages, "create", _fake_create)
    monkeypatch.setattr(cc, "_thinking_supported", True)
    return recorded


_CONTENT = [{"type": "text", "text": "Barcelona Real Madrid"}]


# ─── request shape ────────────────────────────────────────────────────────────

async def test_forecast_uses_adaptive_thinking(calls, temp_db):
    temp_db.db_ensure(880001, "u", "ru")
    reply = await cc.claude_forecast(880001, _CONTENT, "sys", 1400)
    assert reply == "forecast text"
    assert len(calls) == 1
    kw = calls[0]
    assert kw["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in str(kw["thinking"])
    assert kw["output_config"] == {"effort": cc.FORECAST_EFFORT}
    assert kw["model"] == cc.FORECAST_MODEL


async def test_max_tokens_leaves_room_for_thinking(calls, temp_db):
    """Thinking tokens count toward max_tokens together with the answer."""
    temp_db.db_ensure(880002, "u", "ru")
    await cc.claude_forecast(880002, _CONTENT, "sys", 1400)
    assert calls[0]["max_tokens"] == 1400 + cc.THINKING_HEADROOM


async def test_no_sampling_params_sent(calls, temp_db):
    """temperature/top_p/top_k are rejected outright on Opus 4.7+."""
    temp_db.db_ensure(880003, "u", "ru")
    await cc.claude_forecast(880003, _CONTENT, "sys", 1400)
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in calls[0]


# ─── fallback paths ───────────────────────────────────────────────────────────

async def test_bad_request_falls_back_and_stops_retrying(calls, temp_db):
    """A 400 is our config being wrong, not a blip: fall back to a plain call
    once, then stop paying for the doomed round-trip on later forecasts."""
    temp_db.db_ensure(880004, "u", "ru")
    calls.results = [_bad_request(), _Resp("plain reply")]

    reply = await cc.claude_forecast(880004, _CONTENT, "sys", 1400)
    assert reply == "plain reply"
    assert len(calls) == 2
    assert "thinking" in calls[0] and "thinking" not in calls[1]
    assert cc._thinking_supported is False

    # Second forecast: no thinking attempt at all.
    calls.clear()
    calls.results = [_Resp("second reply")]
    assert await cc.claude_forecast(880004, _CONTENT, "sys", 1400) == "second reply"
    assert len(calls) == 1
    assert "thinking" not in calls[0]


async def test_transient_error_falls_back_but_keeps_thinking_enabled(calls, temp_db):
    """A timeout must not permanently disable thinking the way a 400 does."""
    temp_db.db_ensure(880005, "u", "ru")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    calls.results = [anthropic.APITimeoutError(request=request), _Resp("plain reply")]

    reply = await cc.claude_forecast(880005, _CONTENT, "sys", 1400)
    assert reply == "plain reply"
    assert cc._thinking_supported is True


async def test_rate_limit_is_not_swallowed_by_the_fallback(calls, temp_db, monkeypatch):
    """RateLimit keeps surfacing as the localized overload message. It goes
    through the normal transient-retry budget, but must never be retried as a
    PLAIN call — that would just hit the same limit without thinking."""
    import asyncio
    from translations import tr
    temp_db.db_ensure(880006, "u", "ru")

    async def _no_sleep(_):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)   # skip the backoff waits

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    calls.results = [anthropic.RateLimitError("slow down", response=response, body=None)]

    reply = await cc.claude_forecast(880006, _CONTENT, "sys", 1400)
    assert reply == tr(880006, "api_overload")
    assert all("thinking" in kw for kw in calls)       # no plain fallback attempt
    assert cc._thinking_supported is True
