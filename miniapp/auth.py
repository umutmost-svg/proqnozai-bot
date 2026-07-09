"""Telegram Mini App initData validation.

Implements Telegram's documented algorithm for verifying the `initData`
string a Mini App receives from `window.Telegram.WebApp.initData`:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age_s: int = 86400) -> dict | None:
    """Validate a Telegram Mini App initData string.

    Returns the parsed `user` dict on success, or None if the signature is
    invalid, the payload is malformed, or `auth_date` is older than
    `max_age_s` seconds (replay protection).
    """
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = pairs.get("auth_date")
    if not auth_date:
        return None
    try:
        if time.time() - int(auth_date) > max_age_s:
            return None
    except ValueError:
        return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    return user
