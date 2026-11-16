"""Credits and MB ID of Mini App users (docs/miniapp/credits.md).

Every forecast a user generates costs COST credits, charged only after it was generated successfully, once per
item (an AI analysis per match, a quick pick per UTC day and criteria). Every user gets WELCOME credits on first
use and MONTHLY more once per calendar month (UTC) while the balance is below COST. The owner's switch
(`credits.enabled`, set from the admin panel) decides whether forecasts require and spend credits at all.

All writes happen in one transaction on miniapp.db; the journal is append-only and every entry has a unique key,
so a retried request or a re-applied admin command never counts twice.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from miniapp.store import Store

COST = 10
WELCOME = 100
MONTHLY = 50
FLAG_ENABLED = "credits.enabled"
MB_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")  # the admin's own MB ID format (crm.py _MB_ID)
HISTORY = 30


class NoCredits(Exception):
    def __init__(self, balance: int) -> None:
        super().__init__("no_credits")
        self.balance = balance


class MbError(Exception):
    """invalid_mb_id | mb_taken | mb_locked: the message is a stable code."""


@dataclass(frozen=True)
class Account:
    balance: int
    unlimited_until: int | None
    topup_month: str | None


def month_of(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m")


def next_month_start(now: float) -> int:
    d = datetime.fromtimestamp(now, UTC)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp())


class Credits:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    # ---- switches ----------------------------------------------------------------------------

    def enabled(self) -> bool:
        with self.store._connect() as conn:
            row = conn.execute("SELECT value FROM app_flags WHERE key = ?", (FLAG_ENABLED,)).fetchone()
        return row is not None and row[0] == "1"

    @staticmethod
    def _set_flag(conn: sqlite3.Connection, key: str, value: bool, now: int) -> None:
        conn.execute("INSERT INTO app_flags (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                     "SET value = excluded.value, updated_at = excluded.updated_at", (key, "1" if value else "0", now))

    # ---- accounts ----------------------------------------------------------------------------

    @staticmethod
    def _entry(conn: sqlite3.Connection, user_id: int, now: int, kind: str, delta: int, balance: int,
               item: str | None, key: str) -> None:
        conn.execute("INSERT INTO credit_ledger (user_id, at, kind, delta, balance_after, item, key) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, now, kind, delta, balance, item, key))

    def _account(self, conn: sqlite3.Connection, user_id: int, now: int) -> Account:
        """The user's account inside a write transaction: created with the welcome credits on first use, topped up
        once per calendar month while the balance is below one forecast."""
        row = conn.execute("SELECT balance, unlimited_until, topup_month FROM credit_accounts WHERE user_id = ?",
                           (user_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO credit_accounts (user_id, balance, created_at, updated_at) VALUES (?, ?, ?, ?)",
                         (user_id, WELCOME, now, now))
            self._entry(conn, user_id, now, "welcome", WELCOME, WELCOME, None, f"welcome:{user_id}")
            row = (WELCOME, None, None)
        balance, unlimited, topup = row
        month = month_of(now)
        if balance < COST and topup != month:
            balance += MONTHLY
            conn.execute("UPDATE credit_accounts SET balance = ?, topup_month = ?, updated_at = ? WHERE user_id = ?",
                         (balance, month, now, user_id))
            self._entry(conn, user_id, now, "monthly", MONTHLY, balance, None, f"monthly:{user_id}:{month}")
            topup = month
        return Account(balance, unlimited, topup)

    def summary(self, user_id: int) -> dict:
        now = int(self.clock())
        with self.store._write() as conn:
            account = self._account(conn, user_id, now)
            history = conn.execute(
                "SELECT at, kind, delta, balance_after, item FROM credit_ledger WHERE user_id = ? ORDER BY id DESC "
                "LIMIT ?", (user_id, HISTORY)).fetchall()
            mb = conn.execute("SELECT mb_id, state, submitted_at, decided_at, reason FROM mb_links WHERE user_id = ?",
                              (user_id,)).fetchone()
            enabled = conn.execute("SELECT value FROM app_flags WHERE key = ?", (FLAG_ENABLED,)).fetchone()
        unlimited = account.unlimited_until if account.unlimited_until and account.unlimited_until > now else None
        return {
            "enabled": enabled is not None and enabled[0] == "1",
            "balance": account.balance, "cost": COST, "unlimitedUntil": unlimited,
            # The next monthly credits: next month's first day, given only while it would apply (balance below cost).
            "monthly": {"amount": MONTHLY, "nextAt": next_month_start(now) if account.topup_month == month_of(now)
                        and account.balance < COST else None},
            "history": [{"at": a, "kind": k, "delta": d, "balance": b, "item": i} for a, k, d, b, i in history],
            "mb": None if mb is None else {"id": mb[0], "state": mb[1], "submittedAt": mb[2], "decidedAt": mb[3],
                                           "reason": mb[4]},
        }

    # ---- forecasts ---------------------------------------------------------------------------

    def precheck(self, user_id: int, item: str) -> None:
        """Before generating: raises NoCredits when the switch is on, the item is not paid yet, there is no
        unlimited period and the balance is below one forecast. Nothing is charged here."""
        if not self.enabled():
            return
        now = int(self.clock())
        with self.store._write() as conn:
            account = self._account(conn, user_id, now)
            owned = conn.execute("SELECT 1 FROM credit_items WHERE user_id = ? AND item = ?",
                                 (user_id, item)).fetchone()
        if owned is None and not (account.unlimited_until and account.unlimited_until > now) \
                and account.balance < COST:
            raise NoCredits(account.balance)

    def charge(self, user_id: int, item: str) -> bool:
        """After a successful generation: charges COST once per item (True when charged now). With the switch off,
        during an unlimited period or for an item already paid: nothing. A balance that fell below the cost in the
        meantime (a concurrent generation) raises NoCredits; the item stays unpaid."""
        if not self.enabled():
            return False
        now = int(self.clock())
        with self.store._write() as conn:
            account = self._account(conn, user_id, now)
            if conn.execute("SELECT 1 FROM credit_items WHERE user_id = ? AND item = ?", (user_id, item)).fetchone():
                return False
            if account.unlimited_until and account.unlimited_until > now:
                conn.execute("INSERT INTO credit_items (user_id, item, at) VALUES (?, ?, ?)", (user_id, item, now))
                return False
            if account.balance < COST:
                raise NoCredits(account.balance)
            balance = account.balance - COST
            conn.execute("UPDATE credit_accounts SET balance = ?, updated_at = ? WHERE user_id = ?",
                         (balance, now, user_id))
            conn.execute("INSERT INTO credit_items (user_id, item, at) VALUES (?, ?, ?)", (user_id, item, now))
            self._entry(conn, user_id, now, "spend", -COST, balance, item, f"spend:{user_id}:{item}")
        return True

    # ---- MB ID -------------------------------------------------------------------------------

    def submit_mb(self, user_id: int, value: object) -> dict:
        """The user enters their MB ID: pending until the owner decides. An approved ID is final for the user (a
        change goes through the owner); a pending or rejected one may be replaced."""
        if not isinstance(value, str) or not MB_ID.match(value.strip()):
            raise MbError("invalid_mb_id")
        mb_id = value.strip()
        now = int(self.clock())
        with self.store._write() as conn:
            row = conn.execute("SELECT mb_id, state FROM mb_links WHERE user_id = ?", (user_id,)).fetchone()
            if row is not None and row[1] == "approved":
                raise MbError("mb_locked")
            if row is not None and row[1] == "pending" and row[0].lower() == mb_id.lower():
                return {"id": row[0], "state": "pending"}
            taken = conn.execute("SELECT 1 FROM mb_links WHERE mb_id = ? AND state != 'rejected' AND user_id != ?",
                                 (mb_id, user_id)).fetchone()
            if taken:
                raise MbError("mb_taken")
            conn.execute("INSERT INTO mb_links (user_id, mb_id, state, submitted_at) VALUES (?, ?, 'pending', ?) "
                         "ON CONFLICT(user_id) DO UPDATE SET mb_id = excluded.mb_id, state = 'pending', "
                         "submitted_at = excluded.submitted_at, decided_at = NULL, reason = NULL",
                         (user_id, mb_id, now))
        return {"id": mb_id, "state": "pending"}

    # ---- admin commands ----------------------------------------------------------------------

    def apply(self, key: str, kind: str, user_id: int | None, payload: dict) -> str:
        """Applies one admin command once (by its key). Returns a stable result code."""
        now = int(self.clock())
        with self.store._write() as conn:
            done = conn.execute("SELECT result FROM applied_commands WHERE key = ?", (key,)).fetchone()
            if done is not None:
                return done[0]
            result = self._apply(conn, key, kind, user_id, payload, now)
            conn.execute("INSERT INTO applied_commands (key, result, applied_at) VALUES (?, ?, ?)", (key, result, now))
        return result

    def _apply(self, conn: sqlite3.Connection, key: str, kind: str, user_id: int | None, payload: dict,
               now: int) -> str:
        if kind == "flag":
            if payload.get("key") != FLAG_ENABLED or not isinstance(payload.get("value"), bool):
                return "invalid"
            self._set_flag(conn, FLAG_ENABLED, payload["value"], now)
            return "done"
        if not isinstance(user_id, int) or user_id <= 0:
            return "invalid"
        if kind in ("grant", "debit"):
            amount = payload.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= 1_000_000:
                return "invalid"
            account = self._account(conn, user_id, now)
            delta = amount if kind == "grant" else -min(amount, account.balance)  # never below zero
            balance = account.balance + delta
            conn.execute("UPDATE credit_accounts SET balance = ?, updated_at = ? WHERE user_id = ?",
                         (balance, now, user_id))
            self._entry(conn, user_id, now, kind, delta, balance, None, f"admin:{key}")
            return "done"
        if kind == "unlimited":
            days = payload.get("days")
            if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 366:
                return "invalid"
            account = self._account(conn, user_id, now)
            until = now + days * 86400 if days else None
            conn.execute("UPDATE credit_accounts SET unlimited_until = ?, updated_at = ? WHERE user_id = ?",
                         (until, now, user_id))
            self._entry(conn, user_id, now, "unlimited", 0, account.balance, json.dumps({"days": days}), f"admin:{key}")
            return "done"
        if kind in ("mb_approve", "mb_reject"):
            mb_id = payload.get("mbId")
            row = conn.execute("SELECT mb_id, state FROM mb_links WHERE user_id = ?", (user_id,)).fetchone()
            # The decision is for the ID the owner saw: a user who entered another one since needs a new decision.
            if row is None or not isinstance(mb_id, str) or row[0].lower() != mb_id.lower():
                return "stale"
            reason = payload.get("reason") if isinstance(payload.get("reason"), str) else None
            state = "approved" if kind == "mb_approve" else "rejected"
            conn.execute("UPDATE mb_links SET state = ?, decided_at = ?, reason = ? WHERE user_id = ?",
                         (state, now, (reason or "")[:200] or None, user_id))
            return "done"
        if kind == "mb_set":
            mb_id = payload.get("mbId")
            if mb_id in (None, ""):
                conn.execute("DELETE FROM mb_links WHERE user_id = ?", (user_id,))
                return "done"
            if not isinstance(mb_id, str) or not MB_ID.match(mb_id):
                return "invalid"
            if conn.execute("SELECT 1 FROM mb_links WHERE mb_id = ? AND state != 'rejected' AND user_id != ?",
                            (mb_id, user_id)).fetchone():
                return "mb_taken"
            conn.execute("INSERT INTO mb_links (user_id, mb_id, state, submitted_at, decided_at) "
                         "VALUES (?, ?, 'approved', ?, ?) ON CONFLICT(user_id) DO UPDATE SET mb_id = excluded.mb_id, "
                         "state = 'approved', decided_at = excluded.decided_at, reason = NULL",
                         (user_id, mb_id, now, now))
            return "done"
        return "unknown_kind"
