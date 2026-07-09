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


async def claude_forecast(uid: int, msg_content: list, sys_prompt: str, max_tok: int) -> str:
    """
    Call Claude for a forecast, prepending per-user conversation history.
    Saves the completed turn to conversation memory so future requests have context.

    msg_content may include image blocks; only text is persisted to history.
    Transient API errors are retried; on permanent failure a localized message
    is returned instead of raising.
    """
    from translations import tr
    history = db_get_conv(uid)

    # Text-only summary of the current user turn (for storing in history)
    user_text = " ".join(p["text"] for p in msg_content if p.get("type") == "text")

    # Full messages: previous turns (text-only) + current turn (may include images)
    messages = list(history) + [{"role": "user", "content": msg_content}]

    # Extended thinking: the model reasons deeply (weighing form, H2H, injuries,
    # odds value) before writing a concise answer. Budget is separate from the
    # visible output. If the gateway rejects thinking, fall back to a plain call
    # so forecasts never break.
    think_budget = 2500
    try:
        try:
            resp = await _create_with_retry(
                model="claude-opus-4-8",
                max_tokens=max_tok + think_budget,
                system=sys_prompt,
                messages=messages,
                thinking={"type": "enabled", "budget_tokens": think_budget},
            )
        except anthropic.RateLimitError:
            raise  # let the outer handler show the overload message
        except Exception as e:
            # Any other failure from the thinking call → retry once without it,
            # so an unsupported/rejected thinking param never breaks forecasts.
            logger.warning(f"thinking call failed, falling back to plain: {type(e).__name__}: {e}")
            resp = await _create_with_retry(
                model="claude-opus-4-8",
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
        db_save_conv(uid, updated)

        return reply

    except anthropic.RateLimitError:
        return tr(uid, "api_overload")
    except anthropic.APIError as e:
        logger.error(f"claude_forecast APIError: {e} | uid={uid}")
        return tr(uid, "api_error")
    except Exception as e:
        logger.error(f"claude_forecast unexpected error: {e} | uid={uid}", exc_info=True)
        return tr(uid, "api_error")
