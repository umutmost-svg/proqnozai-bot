"""Individual referral links (admin.db) and their attribution.

A link is a code; the bot link is `t.me/<bot>?start=ref_<code>`, the Mini App link
`t.me/<bot>/<app>?startapp=ref_<code>`. Attribution happens on the user's FIRST start only, on the side that
saw it: the bot records it in `bot-crm.db` (BOT_REFERRALS_ENABLED), the Mini App in its own `user_attribution`
table (MINIAPP_REFERRALS_ENABLED). Both are read here; a user attributed on both sides keeps the earlier one.

Link names are free text: they stay in admin.db, the journal records only the link id, code and channel.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import string

from miniapp.admin_web.accounts import ActionError, Admin, AdminStore

PREFIX = "ref_"
CODE = re.compile(r"^[a-z0-9]{3,24}$")
CHANNELS = ("bot", "miniapp")
_ALPHABET = string.ascii_lowercase + string.digits

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_referral_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    channel     TEXT NOT NULL CHECK (channel IN ('bot', 'miniapp')),
    archived_at INTEGER,
    created_at  INTEGER NOT NULL,
    admin_id    INTEGER NOT NULL
);
"""


def link_url(channel: str, code: str, bot_username: str | None, app_name: str | None) -> str | None:
    if not bot_username:
        return None
    if channel == "bot":
        return f"https://t.me/{bot_username}?start={PREFIX}{code}"
    if not app_name:
        return None
    return f"https://t.me/{bot_username}/{app_name}?startapp={PREFIX}{code}"


def source_of(user_id: int, bot_starts: dict[int, dict], app_starts: dict[int, dict],
              links: dict[str, dict]) -> dict | None:
    """The user's acquisition source: the earlier of the bot and Mini App first starts with a referral code."""
    seen = [(s["at"], "bot", s["code"]) for s in [bot_starts.get(user_id)] if s]
    seen += [(s["at"], "miniapp", s["code"]) for s in [app_starts.get(user_id)] if s]
    if not seen:
        return None
    at, channel, code = min(seen)
    link = links.get(code)
    return {"channel": channel, "code": code, "at": at, "linkId": link["id"] if link else None,
            "name": link["name"] if link else None}


class Referrals:
    def __init__(self, accounts: AdminStore) -> None:
        self.accounts = accounts

    def init_schema(self) -> None:
        with self.accounts._connect() as conn:
            conn.executescript(_SCHEMA)

    def links(self) -> list[dict]:
        with self.accounts._connect() as conn:
            rows = conn.execute(
                "SELECT l.id, l.code, l.name, l.channel, l.archived_at, l.created_at, u.email FROM crm_referral_links l "
                "LEFT JOIN admin_users u ON u.id = l.admin_id ORDER BY l.id DESC").fetchall()
        return [{"id": r[0], "code": r[1], "name": r[2], "channel": r[3], "archivedAt": r[4], "createdAt": r[5],
                 "by": r[6]} for r in rows]

    def by_code(self) -> dict[str, dict]:
        return {link["code"]: link for link in self.links()}

    def create(self, actor: Admin, name: object, channel: object, code: object, now: int) -> dict:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ActionError("invalid_name")
        if channel not in CHANNELS:
            raise ActionError("invalid_channel")
        if code in (None, ""):
            code = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        if not isinstance(code, str) or not CODE.match(code):
            raise ActionError("invalid_code_format")
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_referrals")
            try:
                link_id = conn.execute(
                    "INSERT INTO crm_referral_links (code, name, channel, created_at, admin_id) VALUES (?, ?, ?, ?, ?)",
                    (code, name.strip(), channel, now, actor.id)).lastrowid
            except sqlite3.IntegrityError:
                raise ActionError("code_taken", 409) from None
            self.accounts._event(conn, now, actor.id, "referral.create", "referral", str(link_id), None,
                                 {"code": code, "channel": channel}, None, None, "done")
        return {"id": link_id, "code": code}

    def set_archived(self, actor: Admin, link_id: int, archived: object, now: int) -> dict:
        """Archiving hides a link from the active list; its starts keep counting (old links stay in chats)."""
        if not isinstance(archived, bool):
            raise ActionError("invalid_value")
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_referrals")
            changed = conn.execute(
                "UPDATE crm_referral_links SET archived_at = ? WHERE id = ? AND (archived_at IS NULL) = ?",
                (now if archived else None, link_id, archived)).rowcount
            if not changed and conn.execute("SELECT 1 FROM crm_referral_links WHERE id = ?",
                                            (link_id,)).fetchone() is None:
                raise ActionError("not_found", 404)
            if changed:
                self.accounts._event(conn, now, actor.id, "referral.archive", "referral", str(link_id), None,
                                     {"archived": archived}, None, None, "done")
        return {"archived": archived}
