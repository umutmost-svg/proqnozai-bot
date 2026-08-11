"""The Mostbet preload loop must survive a failed fetch.

Before the fix the `while True` body had no exception handling: one failure
killed the task for the process's lifetime, and asyncio only reports a dead
task's exception at garbage-collection time — so the bot silently served a
frozen match list until the next restart. Offline: the fetch is stubbed.
"""
import asyncio

import pytest

import main


class _StopLoop(Exception):
    """Breaks out of the infinite loop once the test has seen enough."""


@pytest.fixture
def run_preload(monkeypatch):
    """Run _preload_mostbet with instant sleeps, stopping after `stop_after`
    fetch attempts. Returns (attempts, delays)."""
    async def _run(results, stop_after):
        attempts = []
        delays = []

        async def _fake_load():
            attempts.append(len(attempts))
            out = results[min(len(attempts) - 1, len(results) - 1)]
            if isinstance(out, Exception):
                raise out
            return out

        async def _fake_sleep(d):
            # The startup delay runs before any attempt; record only the
            # inter-iteration waits so the assertions read clearly.
            if attempts:
                delays.append(d)
            if len(attempts) >= stop_after:
                raise _StopLoop
            return None

        monkeypatch.setattr(main, "_mostbet_load_matches", _fake_load)
        monkeypatch.setattr(main.asyncio, "sleep", _fake_sleep)
        with pytest.raises(_StopLoop):
            await main._preload_mostbet()
        return attempts, delays

    return _run


async def test_loop_survives_a_failing_fetch_and_retries(run_preload):
    attempts, delays = await run_preload(
        [RuntimeError("mostbet down"), ["match"]], stop_after=2)
    assert len(attempts) == 2                       # it tried again
    assert delays[0] == main.PRELOAD_ERROR_BACKOFF  # short backoff after failure


async def test_successful_cycle_keeps_the_normal_ttl(run_preload):
    attempts, delays = await run_preload([["match"]], stop_after=1)
    assert delays == [main.MOSTBET_CACHE_TTL]


async def test_repeated_failures_do_not_kill_the_loop(run_preload):
    attempts, delays = await run_preload([RuntimeError("still down")], stop_after=3)
    assert len(attempts) == 3
    assert delays[:2] == [main.PRELOAD_ERROR_BACKOFF] * 2


async def test_cancellation_is_not_swallowed(monkeypatch):
    """Shutdown must propagate — CancelledError is not a failure to retry."""
    async def _cancelled():
        raise asyncio.CancelledError

    async def _fake_sleep(_):
        return None

    monkeypatch.setattr(main, "_mostbet_load_matches", _cancelled)
    monkeypatch.setattr(main.asyncio, "sleep", _fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await main._preload_mostbet()
