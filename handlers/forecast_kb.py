"""Pure keyboard construction and time/label formatting for the forecast menu.

Extracted from handlers/forecast.py so the (stateful, async) menu handlers stay
separate from the pure "view" layer. Everything here is synchronous and free of
context.user_data — given a frozen list + a page it returns an
InlineKeyboardMarkup, so it is trivially unit-testable. The handlers in
forecast.py import these builders; nothing here imports back (one-directional).
"""
from datetime import date, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from db import db_get_tz
from translations import tr
from event_list import (
    paginate, PAGE_SIZE, COUNTRY_INTERNATIONAL, DAY_LIVE, DAY_TODAY, DAY_TOMORROW,
)
from handlers.utils import _sport_emoji


def _user_tz(uid: int) -> timezone:
    """The user's timezone (from their stored offset). Display uses this; all
    internal comparisons stay in UTC."""
    return timezone(timedelta(hours=db_get_tz(uid) or 0))


def _fmt_kickoff(dt_utc, uid: int) -> str:
    """Format a tz-aware UTC kickoff in the user's local timezone."""
    if dt_utc is None:
        return ""
    off = db_get_tz(uid) or 0
    local = dt_utc.astimezone(timezone(timedelta(hours=off)))
    sign = "+" if off >= 0 else "-"
    return local.strftime("%d.%m %H:%M") + f" (UTC{sign}{abs(off)})"


def _parse_index(data: str) -> int | None:
    """Parse the trailing integer from callback_data (e.g. "fm_day_3" -> 3).
    Returns None on malformed data (a hand-crafted or replayed callback)
    instead of raising, so callers can degrade to the same expired-menu path
    used for an out-of-range index."""
    try:
        return int(data.split("_")[2])
    except (IndexError, ValueError):
        return None


def _match_label(it, uid: int) -> str:
    """Button label: live state or localized day/time, then the teams."""
    if it.is_live or it.status == "live":
        prefix = "🔴 LIVE"
    else:
        t = _fmt_kickoff(it.kickoff_utc, uid)
        prefix = ("⏸ " + t) if it.postponed else t
    # Team names get a bit more room before truncation so common long names
    # ("Borussia M'gladbach") aren't chopped mid-word; Telegram wraps a long
    # button label rather than clipping it.
    return f"{prefix}  {it.home[:22]} — {it.away[:22]}".strip()


def _build_sport_kb(sport_groups: list, page: int, uid: int) -> InlineKeyboardMarkup:
    """Top-level sport selector, one page of the frozen ordered
    [(sport, items)] list. Button indices are the ABSOLUTE position in the
    full list, same rationale as _build_league_kb/_build_match_kb."""
    page_groups, page, has_prev, has_next = paginate(sport_groups, page, PAGE_SIZE)
    offset = page * PAGE_SIZE
    btns = []
    for i, (cat, items) in enumerate(page_groups, start=offset):
        emoji = _sport_emoji(cat)
        btns.append([InlineKeyboardButton(f"{emoji} {cat} ({len(items)})",
                                          callback_data=f"fm_sp_{i}")])
    btns.extend(_pagination_rows(uid, page, has_prev, has_next, "fm_sppg_",
                                 _total_pages(len(sport_groups))))
    return InlineKeyboardMarkup(btns)


# Country/region (lineSuperCategory, English) → flag emoji. Falls back to 🏆.
_COUNTRY_FLAG = {
    "england": "🏴", "spain": "🇪🇸", "germany": "🇩🇪", "italy": "🇮🇹",
    "france": "🇫🇷", "netherlands": "🇳🇱", "portugal": "🇵🇹", "belgium": "🇧🇪",
    "turkey": "🇹🇷", "russia": "🇷🇺", "ukraine": "🇺🇦", "scotland": "🏴",
    "greece": "🇬🇷", "austria": "🇦🇹", "switzerland": "🇨🇭", "poland": "🇵🇱",
    "denmark": "🇩🇰", "norway": "🇳🇴", "sweden": "🇸🇪", "czech republic": "🇨🇿",
    "croatia": "🇭🇷", "serbia": "🇷🇸", "romania": "🇷🇴", "hungary": "🇭🇺",
    "ireland": "🇮🇪", "wales": "🏴", "finland": "🇫🇮", "bulgaria": "🇧🇬",
    "usa": "🇺🇸", "united states": "🇺🇸", "mexico": "🇲🇽", "brazil": "🇧🇷",
    "argentina": "🇦🇷", "chile": "🇨🇱", "colombia": "🇨🇴", "uruguay": "🇺🇾",
    "japan": "🇯🇵", "south korea": "🇰🇷", "china": "🇨🇳", "australia": "🇦🇺",
    "saudi arabia": "🇸🇦", "qatar": "🇶🇦", "uae": "🇦🇪", "egypt": "🇪🇬",
    "morocco": "🇲🇦", "azerbaijan": "🇦🇿", "kazakhstan": "🇰🇿", "uzbekistan": "🇺🇿",
    "georgia": "🇬🇪", "israel": "🇮🇱", "iran": "🇮🇷", "india": "🇮🇳",
    "south africa": "🇿🇦", "nigeria": "🇳🇬", "ecuador": "🇪🇨", "peru": "🇵🇪",
    "paraguay": "🇵🇾", "bolivia": "🇧🇴", "venezuela": "🇻🇪", "canada": "🇨🇦",
    "slovakia": "🇸🇰", "slovenia": "🇸🇮", "cyprus": "🇨🇾", "iceland": "🇮🇸",
    # Regions / international
    "international": "🌍", "world": "🌍", "europe": "🇪🇺", "europa": "🇪🇺",
    "south america": "🌎", "asia": "🌏", "africa": "🌍", "north america": "🌎",
    "club friendlies": "🤝", "friendlies": "🤝",
}


def _country_flag(country: str) -> str:
    return _COUNTRY_FLAG.get((country or "").strip().lower(), "🏆")


def _total_pages(n: int) -> int:
    return max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)


def _home_back_row(uid: int, back_cb: str) -> list:
    """Bottom row for deep screens: a "🏠 to start" shortcut (jump straight to
    the sport list from anywhere) alongside the one-step "back". When back
    already targets the sport list, the two would be identical — show just back."""
    back_btn = InlineKeyboardButton(tr(uid, "ev_back"), callback_data=back_cb)
    if back_cb == "fm_back_sport":
        return [back_btn]
    return [InlineKeyboardButton(tr(uid, "ev_home"), callback_data="fm_back_sport"), back_btn]


def _pagination_rows(uid: int, page: int, has_prev: bool, has_next: bool,
                     prefix: str, total_pages: int) -> list:
    """Prev/next row plus a centered "page X / Y" counter row so the user can
    see how deep the list is. Returns [] on a single page. The counter uses a
    no-op callback (fm_noop) — it's a read-only indicator, not a control."""
    if not (has_prev or has_next):
        return []
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton(tr(uid, "ev_page_prev"), callback_data=f"{prefix}{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(tr(uid, "ev_page_more"), callback_data=f"{prefix}{page+1}"))
    counter = [InlineKeyboardButton(f"{page + 1} / {total_pages}", callback_data="fm_noop")]
    return [nav, counter]


def _build_league_kb(groups: list, page: int, back_cb: str, uid: int) -> InlineKeyboardMarkup:
    """Tournament selector, one page of the frozen ordered LeagueGroup list.
    Names are shown as Mostbet supplies them; a country flag aids scanning.
    Button indices are the ABSOLUTE position in the full list — pagination
    only changes which slice is shown, never what an index resolves to."""
    page_groups, page, has_prev, has_next = paginate(groups, page, PAGE_SIZE)
    offset = page * PAGE_SIZE
    btns = []
    for i, g in enumerate(page_groups, start=offset):
        flag = _country_flag(g.country or "")
        label = f"{flag} {g.league_name}"
        if g.country and flag == "🏆" and g.country.lower() not in g.league_name.lower():
            label += f" · {g.country}"
        label += f" ({len(g.items)})"
        btns.append([InlineKeyboardButton(label, callback_data=f"fm_lg_{i}")])
    btns.extend(_pagination_rows(uid, page, has_prev, has_next, "fm_lgpg_",
                                 _total_pages(len(groups))))
    btns.append(_home_back_row(uid, back_cb))
    return InlineKeyboardMarkup(btns)


def _build_match_kb(matches: list, page: int, uid: int) -> InlineKeyboardMarkup:
    """Match selector, one page of the frozen ordered match list. Button
    indices are the ABSOLUTE position in the full list, same rationale as
    _build_league_kb."""
    page_matches, page, has_prev, has_next = paginate(matches, page, PAGE_SIZE)
    offset = page * PAGE_SIZE
    btns = [[InlineKeyboardButton(_match_label(it, uid), callback_data=f"fm_mt_{i}")]
            for i, it in enumerate(page_matches, start=offset)]
    btns.extend(_pagination_rows(uid, page, has_prev, has_next, "fm_mtpg_",
                                 _total_pages(len(matches))))
    btns.append(_home_back_row(uid, "fm_back_league"))
    return InlineKeyboardMarkup(btns)


def _day_label(day_key: str, count: int, uid: int) -> str:
    if day_key == DAY_LIVE:
        return f"🔴 LIVE ({count})"
    if day_key == DAY_TODAY:
        return f"{tr(uid, 'ev_day_today')} ({count})"
    if day_key == DAY_TOMORROW:
        return f"{tr(uid, 'ev_day_tomorrow')} ({count})"
    return f"{date.fromisoformat(day_key).strftime('%d.%m')} ({count})"


def _build_day_kb(day_options: list, uid: int) -> InlineKeyboardMarkup:
    """Day filter selector. Index 0 is always the fixed "all days" option;
    index i (1-based) resolves to day_options[i-1] — day_options never needs
    its own pagination (at most LIVE/TODAY/TOMORROW + up to 7 dated days)."""
    btns = [[InlineKeyboardButton(tr(uid, "ev_day_all"), callback_data="fm_day_0")]]
    for i, (key, count) in enumerate(day_options, start=1):
        btns.append([InlineKeyboardButton(_day_label(key, count, uid), callback_data=f"fm_day_{i}")])
    btns.append([InlineKeyboardButton(tr(uid, "ev_back"), callback_data="fm_back_sport")])
    return InlineKeyboardMarkup(btns)


def _build_country_kb(country_options: list, page: int, uid: int,
                      back_cb: str = "fm_back_day") -> InlineKeyboardMarkup:
    """Country/region filter selector, paginated. Index 0 is always the fixed
    "all countries" option (shown only on page 0); index i (1-based) is the
    ABSOLUTE position in the full country_options list. `back_cb` is the target
    of the back button — normally the day screen, but the sport screen when the
    day step was auto-skipped (single day option)."""
    page_opts, page, has_prev, has_next = paginate(country_options, page, PAGE_SIZE)
    offset = page * PAGE_SIZE
    btns = []
    if page == 0:
        btns.append([InlineKeyboardButton(tr(uid, "ev_country_all"), callback_data="fm_ctry_0")])
    for i, (key, count) in enumerate(page_opts, start=offset):
        flag = "🌍" if key == COUNTRY_INTERNATIONAL else _country_flag(key)
        btns.append([InlineKeyboardButton(f"{flag} {key} ({count})", callback_data=f"fm_ctry_{i + 1}")])
    btns.extend(_pagination_rows(uid, page, has_prev, has_next, "fm_ctrypg_",
                                 _total_pages(len(country_options))))
    btns.append(_home_back_row(uid, back_cb))
    return InlineKeyboardMarkup(btns)
