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


# --- Ed25519 (third-party validation): a test key pair stands in for Telegram's --------------------
import base64  # noqa: E402

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

TEST_BOT_ID = 7000000001
TEST_ED25519_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
TEST_ED25519_PUBLIC_HEX = TEST_ED25519_KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def ed25519_signature(fields: dict[str, str], bot_id: int = TEST_BOT_ID, key: Ed25519PrivateKey = TEST_ED25519_KEY) -> str:
    """Built from the documentation, independently of the validator."""
    lines = sorted(f"{k}={v}" for k, v in fields.items() if k not in ("hash", "signature"))
    message = (f"{bot_id}:WebAppData\n" + "\n".join(lines)).encode("utf-8")
    return base64.urlsafe_b64encode(key.sign(message)).rstrip(b"=").decode()


def make_init_data_ed25519(
    user: dict | None = None,
    *,
    auth_date: int,
    bot_id: int = TEST_BOT_ID,
    key: Ed25519PrivateKey = TEST_ED25519_KEY,
    extra: dict[str, str] | None = None,
    with_hash: bool = True,
) -> str:
    """What a Telegram client sends: user, auth_date, signature, and the bot-token `hash` on top."""
    fields = {"query_id": "AAHtestQuery", "auth_date": str(auth_date)}
    fields["user"] = user_json(user if user is not None else {"id": 123456789, "first_name": "Test"})
    fields.update(extra or {})
    fields["signature"] = ed25519_signature(fields, bot_id, key)
    if with_hash:
        fields["hash"] = sign(fields)
    return encode(fields)
