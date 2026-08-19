"""
Broadcast service: validation, sending, and the scheduler for delayed sends.

The dashboard and the stats server stay thin — they hand a payload here and read
progress back. Everything that decides *what* is a valid broadcast and *how* it
reaches Telegram lives in this module.

A broadcast is one row in `broadcasts` (db.py) from the moment it is queued, so
a scheduled send survives a worker restart: the row is still pending after the
redeploy and the scheduler picks it up on its next tick.
"""
import asyncio
import html
import json
import logging
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from db import (db_segment_uids, db_create_broadcast, db_due_broadcasts,
                db_claim_broadcast, db_broadcast_progress, db_finish_broadcast,
                db_get_broadcast)

logger = logging.getLogger(__name__)

# Telegram's HTML subset. Anything outside this list is rejected before the
# broadcast starts: Telegram fails the whole send with "can't parse entities",
# and discovering that per recipient would mean a broadcast that reaches nobody.
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "blockquote", "span", "tg-spoiler",
}
# Tags Telegram treats as self-contained; everything else must be balanced.
VOID_TAGS = {"br"}

ALLOWED_URL_SCHEMES = ("http://", "https://", "tg://")

MAX_TEXT = 4096
MAX_BUTTON_ROWS = 8
MAX_BUTTONS_PER_ROW = 3
MAX_BUTTON_TEXT = 64

# ~20 messages/second, comfortably under Telegram's 30/s ceiling for broadcasts.
SEND_DELAY = 0.05
# How often the DB row is refreshed while sending. Per-message writes would turn
# one broadcast into thousands of SQLite transactions for no added insight.
PROGRESS_EVERY = 25
# Scheduler tick. Minute-level accuracy is what the UI promises, so a 30s tick
# leaves the send at most half a minute late.
SCHEDULER_TICK = 30

# Admin-facing times are Moscow; the DB and every comparison stay UTC.
ADMIN_TZ = timezone(timedelta(hours=3))

# Progress of the current/last broadcast, served by GET /broadcast/status.
state: dict = {"running": False, "ok": 0, "fail": 0, "total": 0, "done": False, "id": 0}


class _Validator(HTMLParser):
    """Checks Telegram's HTML subset: known tags, balanced, usable links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.error: str | None = None

    def handle_starttag(self, tag, attrs):
        if self.error:
            return
        if tag in VOID_TAGS:
            return
        if tag not in ALLOWED_TAGS:
            self.error = f"Тег &lt;{tag}&gt; не поддерживается Telegram"
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if not href.startswith(ALLOWED_URL_SCHEMES):
                self.error = "Ссылка должна начинаться с http://, https:// или tg://"
                return
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.error or tag in VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.error = f"Незакрытый или лишний тег &lt;/{tag}&gt;"
            return
        self.stack.pop()

    def finish(self) -> str | None:
        if self.error:
            return self.error
        if self.stack:
            return f"Не закрыт тег &lt;{self.stack[-1]}&gt;"
        return None


def validate_text(text: str) -> str | None:
    """None when the text is sendable, otherwise a message for the operator."""
    if not text.strip():
        return "Пустой текст"
    if len(text) > MAX_TEXT:
        return f"Слишком длинно: {len(text)} символов, максимум {MAX_TEXT}"
    v = _Validator()
    try:
        v.feed(text)
        v.close()
    except Exception:
        return "Не удалось разобрать HTML-разметку"
    return v.finish()


def parse_buttons(raw) -> tuple[list[list[dict]], str | None]:
    """Normalise the button payload to rows of {text, url}.

    Accepts the JSON the dashboard posts (list of rows) and, for convenience, a
    flat list — one button per row."""
    if not raw:
        return [], None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            return [], "Кнопки: некорректный JSON"
    if not isinstance(raw, list):
        return [], "Кнопки: ожидается список рядов"

    rows: list[list[dict]] = []
    for item in raw:
        row = item if isinstance(item, list) else [item]
        out_row = []
        for btn in row:
            if not isinstance(btn, dict):
                return [], "Кнопки: каждая кнопка — объект {text, url}"
            text = str(btn.get("text") or "").strip()
            url = str(btn.get("url") or "").strip()
            if not text or not url:
                continue  # a half-filled row in the UI is simply ignored
            if len(text) > MAX_BUTTON_TEXT:
                return [], f"Кнопка «{text[:20]}…»: текст длиннее {MAX_BUTTON_TEXT} символов"
            if not url.startswith(ALLOWED_URL_SCHEMES):
                return [], f"Кнопка «{text}»: ссылка должна начинаться с http://, https:// или tg://"
            out_row.append({"text": text, "url": url})
        if not out_row:
            continue
        if len(out_row) > MAX_BUTTONS_PER_ROW:
            return [], f"В ряду не больше {MAX_BUTTONS_PER_ROW} кнопок"
        rows.append(out_row)
    if len(rows) > MAX_BUTTON_ROWS:
        return [], f"Не больше {MAX_BUTTON_ROWS} рядов кнопок"
    return rows, None


def parse_run_at(value: str) -> tuple[str, str | None]:
    """Admin-entered local time (Moscow) → UTC 'YYYY-MM-DD HH:MM:SS'.

    Empty means "send now". A past time is rejected rather than silently fired:
    a mistyped date should not turn a scheduled send into an immediate one."""
    value = (value or "").strip()
    if not value:
        return "", None
    try:
        local = datetime.fromisoformat(value.replace("T", " "))
    except ValueError:
        return "", "Некорректная дата отправки"
    if local.tzinfo is None:
        local = local.replace(tzinfo=ADMIN_TZ)
    utc = local.astimezone(timezone.utc)
    # One minute of slack: submitting "in a moment" must not trip the past check.
    if utc < datetime.now(timezone.utc) - timedelta(minutes=1):
        return "", "Время отправки уже прошло"
    return utc.strftime("%Y-%m-%d %H:%M:%S"), None


def utc_to_admin(value: str) -> str:
    """UTC timestamp from the DB → 'DD.MM HH:MM' in the admin's timezone."""
    if not value:
        return ""
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return value
    return dt.astimezone(ADMIN_TZ).strftime("%d.%m %H:%M")


def queue(text: str, segment: str, buttons=None, no_preview: bool = False,
          run_at_local: str = "") -> tuple[dict | None, str | None]:
    """Validate a broadcast request and persist it. Returns (info, error).

    Validation happens here, before anything is stored, so a broken message is
    rejected while the operator is still looking at the form — not two hours
    later when a scheduled send fails against Telegram."""
    err = validate_text(text)
    if err:
        return None, err
    rows, err = parse_buttons(buttons)
    if err:
        return None, err
    run_at, err = parse_run_at(run_at_local)
    if err:
        return None, err

    uids = db_segment_uids(segment)
    if not uids:
        return None, "В выбранном сегменте нет получателей"

    bid = db_create_broadcast(
        text=text, segment=segment,
        buttons=json.dumps(rows, ensure_ascii=False) if rows else "",
        parse_mode="HTML", no_preview=int(bool(no_preview)), run_at=run_at)
    return {"id": bid, "started": len(uids), "recipients": len(uids),
            "run_at": run_at, "scheduled": bool(run_at)}, None


def _markup(buttons_json: str):
    """Inline keyboard for a stored broadcast, or None. Imported lazily so the
    dashboard process can use validation without python-telegram-bot."""
    if not buttons_json:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    try:
        rows = json.loads(buttons_json)
    except ValueError:
        return None
    kb = [[InlineKeyboardButton(b["text"], url=b["url"]) for b in row] for row in rows]
    return InlineKeyboardMarkup(kb) if kb else None


async def run_broadcast(bot, bid: int) -> None:
    """Send one claimed broadcast. Never raises: the scheduler must keep running
    whatever happens to a single campaign."""
    row = db_get_broadcast(bid)
    if not row:
        return
    uids = db_segment_uids(row["segment"])
    markup = _markup(row["buttons"])
    ok = fail = 0
    total = len(uids)
    state.update(running=True, ok=0, fail=0, total=total, done=False, id=bid)
    db_broadcast_progress(bid, total, 0, 0)

    try:
        for i, uid in enumerate(uids):
            try:
                await _send_one(bot, uid, row, markup)
                ok += 1
            except Exception:
                fail += 1
            state.update(ok=ok, fail=fail)
            if i % PROGRESS_EVERY == PROGRESS_EVERY - 1:
                db_broadcast_progress(bid, total, ok, fail)
            await asyncio.sleep(SEND_DELAY)
        db_finish_broadcast(bid, ok, fail)
    except Exception as e:
        logger.exception("broadcast %s aborted", bid)
        db_finish_broadcast(bid, ok, fail, error=type(e).__name__)
    finally:
        state.update(running=False, done=True, ok=ok, fail=fail, total=total)
        logger.info("broadcast %s finished: ok=%s fail=%s total=%s", bid, ok, fail, total)


async def _send_one(bot, uid: int, row: dict, markup) -> None:
    """One message, with a single retry when Telegram asks us to slow down.

    Flood control is the one failure worth retrying: it is temporary and hits
    exactly when a broadcast is large enough to matter. A blocked chat or a
    deleted account is permanent and counts as a failure immediately."""
    from telegram.error import RetryAfter
    try:
        await bot.send_message(
            chat_id=uid, text=row["text"], parse_mode=row["parse_mode"] or None,
            reply_markup=markup,
            disable_web_page_preview=bool(row["no_preview"]))
    except RetryAfter as e:
        await asyncio.sleep(min(float(getattr(e, "retry_after", 5)) + 1, 60))
        await bot.send_message(
            chat_id=uid, text=row["text"], parse_mode=row["parse_mode"] or None,
            reply_markup=markup,
            disable_web_page_preview=bool(row["no_preview"]))


async def scheduler(application) -> None:
    """Pick up due broadcasts, one at a time, forever.

    Sends are serialised deliberately: two concurrent campaigns would share the
    same Telegram rate limit and both would be throttled."""
    await asyncio.sleep(15)  # let the bot finish starting up
    while True:
        try:
            if not state["running"]:
                for row in db_due_broadcasts():
                    if db_claim_broadcast(row["id"]):
                        await run_broadcast(application.bot, row["id"])
                        break  # re-check timing before starting the next one
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("broadcast scheduler tick failed")
        await asyncio.sleep(SCHEDULER_TICK)


def escape(text: str) -> str:
    """Escape text that must appear literally inside an HTML broadcast."""
    return html.escape(text, quote=False)
