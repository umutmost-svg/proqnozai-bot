"""Test-only initData signer. Lives in the test runner, never in runtime code.

Deliberately independent from miniapp.telegram_auth.compute_hash so the
validator is not checked against its own helper.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import quote, quote_plus

TEST_BOT_TOKEN = "7000000001:AAtest-only-not-a-real-token"


def user_json(user: dict) -> str:
    return json.dumps(user, ensure_ascii=False, separators=(",", ":"))


def sign(fields: dict[str, str], token: str = TEST_BOT_TOKEN) -> str:
    lines = sorted(f"{key}={value}" for key, value in fields.items())
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret, "\n".join(lines).encode("utf-8"), hashlib.sha256).hexdigest()


def encode(fields: dict[str, str], *, plus_for_space: bool = False) -> str:
    q = quote_plus if plus_for_space else (lambda v: quote(v, safe=""))
    return "&".join(f"{q(k)}={q(v)}" for k, v in fields.items())


def make_init_data(
    user: dict | None = None,
    *,
    auth_date: int,
    token: str = TEST_BOT_TOKEN,
    extra: dict[str, str] | None = None,
    plus_for_space: bool = False,
) -> str:
    fields = {"query_id": "AAHtestQuery", "auth_date": str(auth_date)}
    fields["user"] = user_json(user if user is not None else {"id": 123456789, "first_name": "Test"})
    fields.update(extra or {})
    fields["hash"] = sign(fields, token)
    return encode(fields, plus_for_space=plus_for_space)
