import asyncio
import logging

import anthropic

from config import ANTHROPIC_KEY
from db import db_get_conv, db_save_conv, db_lang

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
request_semaphore = asyncio.Semaphore(5)


async def live_tip(uid: int, match: str, minute: int, score: str, event: str) -> str:
    """Generate a short live betting tip using Claude Haiku."""
    try:
        from translations import T
        lang = db_lang(uid)
        p = T[lang]["live_tip_prompt"].format(match=match, minute=minute, score=score, event=event)
        r = await _create_with_retry(
            model="claude-haiku-4-5-20251001", max_tokens=150,
            messages=[{"role": "user", "content": p}]
        )
        if not r.content:
            return ""
        return r.content[0].text
    except Exception as e:
        logger.warning(f"live_tip: {e}")
        return ""


# Transient errors worth retrying with backoff
_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)


async def _create_with_retry(*, max_retries: int = 2, **kwargs):
    """Call client.messages.create with exponential backoff on transient errors.
    Raises the last exception if all retries fail."""
    delay = 1.0
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            async with request_semaphore:
                return await asyncio.to_thread(client.messages.create, **kwargs)
        except _RETRYABLE as e:
            last_exc = e
            if attempt < max_retries:
                logger.warning(f"Claude transient error (attempt {attempt+1}): {type(e).__name__}; retry in {delay}s")
                await asyncio.sleep(delay)
                delay *= 2
            else:
                raise
    raise last_exc  # pragma: no cover


FORECAST_MODEL = "claude-opus-4-8"

# Adaptive thinking is the ONLY thinking mode Opus 4.7+ accepts: the legacy
# {"type": "enabled", "budget_tokens": N} form is rejected with a 400
# ("thinking.type.enabled is not supported for this model"). Depth is steered by
# output_config.effort instead of a token budget. "medium" keeps spend close to
# the ~2500-token budget this call used to ask for; the API default is "high".
FORECAST_EFFORT = "medium"
# Thinking tokens count toward max_tokens together with the answer, so the cap
# needs headroom on top of the visible-answer budget or the reply truncates.
THINKING_HEADROOM = 2500

# Flipped off after the API rejects our thinking configuration outright. A 400 is
# a bug in what we send, not a transient failure — retrying it on every forecast
# would burn a round-trip per request forever, so the process stops asking.
_thinking_supported = True


def _disable_thinking(reason: str) -> None:
    global _thinking_supported
    _thinking_supported = False
    logger.error(
        "Adaptive thinking rejected by the API — falling back to plain calls for "
        "the rest of this process. Fix the request config: %s", reason)


async def claude_forecast(uid: int, msg_content: list, sys_prompt: str, max_tok: int,
                          fixture_key: str = "") -> str:
    """
    Call Claude for a forecast, prepending per-user conversation history.
    Saves the completed turn to conversation memory so future requests have context.

    `fixture_key` scopes that memory to one match: a request carrying a different
    key starts a fresh context, an empty key (a follow-up question that names no
    fixture) continues the stored one. See db.db_get_conv.

    msg_content may include image blocks; only text is persisted to history.
    Transient API errors are retried; on permanent failure a localized message
    is returned instead of raising.
    """
    from translations import tr
    history = db_get_conv(uid, fixture_key)

    # Text-only summary of the current user turn (for storing in history)
    user_text = " ".join(p["text"] for p in msg_content if p.get("type") == "text")

    # Full messages: previous turns (text-only) + current turn (may include images)
    messages = list(history) + [{"role": "user", "content": msg_content}]

    # Adaptive thinking: the model decides how deeply to reason (weighing form,
    # H2H, injuries, odds value) before writing a concise answer. If the call
    # fails for any reason, fall back to a plain one so forecasts never break.
    try:
        resp = None
        if _thinking_supported:
            try:
                resp = await _create_with_retry(
                    model=FORECAST_MODEL,
                    max_tokens=max_tok + THINKING_HEADROOM,
                    system=sys_prompt,
                    messages=messages,
                    thinking={"type": "adaptive"},
                    output_config={"effort": FORECAST_EFFORT},
                )
            except anthropic.RateLimitError:
                raise  # let the outer handler show the overload message
            except anthropic.BadRequestError as e:
                # Our request shape is wrong — the same call will fail forever.
                _disable_thinking(f"{type(e).__name__}: {e}")
            except Exception as e:
                # Transient or unexpected (already retried inside
                # _create_with_retry) — keep thinking on for the next forecast.
                logger.warning(f"thinking call failed, falling back to plain: {type(e).__name__}: {e}")
        if resp is None:
            resp = await _create_with_retry(
                model=FORECAST_MODEL,
                max_tokens=max_tok,
                system=sys_prompt,
                messages=messages,
            )
        # Pick the text block (with thinking, content[0] is a thinking block).
        reply = next((b.text for b in (resp.content or [])
                      if getattr(b, "type", "") == "text" and getattr(b, "text", "")), "")
        if not reply:
            logger.error(f"claude_forecast empty response | uid={uid}")
            return tr(uid, "api_error")
        logger.info(f"claude_forecast OK | uid={uid}")

        # Persist this turn as text-only so next call has context
        updated = list(history) + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply},
        ]
        db_save_conv(uid, updated, fixture_key)

        return reply

    except anthropic.RateLimitError:
        return tr(uid, "api_overload")
    except anthropic.APIError as e:
        logger.error(f"claude_forecast APIError: {e} | uid={uid}")
        return tr(uid, "api_error")
    except Exception as e:
        logger.error(f"claude_forecast unexpected error: {e} | uid={uid}", exc_info=True)
        return tr(uid, "api_error")
