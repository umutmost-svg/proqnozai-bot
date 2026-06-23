import asyncio
import json
import logging
import re

import anthropic

from config import ANTHROPIC_KEY
from db import db_get_conv, db_save_conv, db_lang

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
request_semaphore = asyncio.Semaphore(5)


async def parse_match_query(text: str, lang: str) -> dict:
    """Use Claude Haiku to extract team names and date from user query."""
    try:
        prompt = (
            f'Extract match info from this text: "{text}"\n'
            'Return JSON only, no explanation:\n'
            '{"team1": "...", "team2": "...", "date": "DD.MM.YYYY or null", '
            '"sport": "football/basketball/ufc/tennis/other"}\n'
            'If you cannot find two teams, return '
            '{"team1": null, "team2": null, "date": null, "sport": "football"}'
        )
        r = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001", max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = r.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        logger.error(f"parse_match_query: {e}")
    return {"team1": None, "team2": None, "date": None, "sport": "football"}


async def live_tip(uid: int, match: str, minute: int, score: str, event: str) -> str:
    """Generate a short live betting tip using Claude Haiku."""
    try:
        from translations import T
        lang = db_lang(uid)
        p = T[lang]["live_tip_prompt"].format(match=match, minute=minute, score=score, event=event)
        r = await asyncio.to_thread(
            client.messages.create, model="claude-haiku-4-5-20251001", max_tokens=150,
            messages=[{"role": "user", "content": p}]
        )
        return r.content[0].text
    except Exception:
        return ""


async def claude_forecast(uid: int, msg_content: list, sys_prompt: str, max_tok: int) -> str:
    """
    Call Claude Sonnet for a forecast, prepending per-user conversation history.
    Saves the completed turn to conversation memory so future requests have context.

    msg_content may include image blocks; only text is persisted to history.
    """
    history = db_get_conv(uid)

    # Text-only summary of the current user turn (for storing in history)
    user_text = " ".join(p["text"] for p in msg_content if p.get("type") == "text")

    # Full messages: previous turns (text-only) + current turn (may include images)
    messages = list(history) + [{"role": "user", "content": msg_content}]

    try:
        async with request_semaphore:
            resp = await asyncio.to_thread(
                client.messages.create,
                model="claude-opus-4-8",
                max_tokens=max_tok,
                system=sys_prompt,
                messages=messages,
            )
        reply = resp.content[0].text
        logger.info(f"claude_forecast OK | uid={uid} tok={max_tok}")

        # Persist this turn as text-only so next call has context
        updated = list(history) + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply},
        ]
        db_save_conv(uid, updated)

        return reply

    except anthropic.RateLimitError:
        from translations import tr
        return tr(uid, "api_overload")
    except anthropic.APIError as e:
        logger.error(f"claude_forecast APIError: {e} | uid={uid}")
        from translations import tr
        return tr(uid, "api_error")
