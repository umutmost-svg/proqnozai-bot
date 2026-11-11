"""Validation of Telegram Mini App `initData`: two algorithms, one per deployment.

HMAC (bot-token based; https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
data-check-string = every received field except `hash`, as `key=<value>`
(URL-decoded values, exactly as received), sorted by key, joined by "\\n";
secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token);
hash = hex(HMAC_SHA256(key=secret_key, msg=data-check-string)).
`signature` is an ordinary received field for this check.

Ed25519 (third-party validation; https://core.telegram.org/bots/webapps#validating-data-for-third-party-use):
no bot token at all. data-check-string = "<bot_id>:WebAppData\\n" + every received field except `hash`
and `signature`, sorted, `key=<value>` joined by "\\n"; `signature` = base64url Ed25519 signature of it by
Telegram's public key. `hash` is NOT covered by this signature, so nothing may rely on it in this mode:
the launch id is the SHA-256 of the verified data-check-string.

Errors carry only a reason code: the payload, user data and token never end
up in exception messages or logs.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from urllib.parse import unquote_plus

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Telegram: user ids "may have more than 32 significant bits" but fit a double.
MAX_TELEGRAM_ID = 2**53 - 1

_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTH_DATE_RE = re.compile(r"^[0-9]{1,12}$")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}(==)?$")  # 64 bytes, base64url, padding optional
# Telegram's Ed25519 keys for third-party validation (hex, from the Mini Apps documentation).
TELEGRAM_PUBLIC_KEYS = {
    "production": "e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d",
    "test": "40055058a4ee38156a06562e52eece92a771bcd8346a8c4615cb7376eddf72ec",
}
_STR_FIELDS = ("first_name", "last_name", "username", "language_code", "photo_url")
_BOOL_FIELDS = ("is_bot", "is_premium", "added_to_attachment_menu", "allows_write_to_pm")


class InitDataError(Exception):
    """initData rejected. `code` is safe to log and to return to the client."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def expired(self) -> bool:
        return self.code in ("expired", "from_future")


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str | None
    last_name: str | None
    username: str | None
    language_code: str | None


@dataclass(frozen=True)
class VerifiedInitData:
    user: TelegramUser
    auth_date: int
    # Unique per signed payload, used to detect reuse after logout: the received `hash` (HMAC
    # mode, where it is the signature) or SHA-256 of the Ed25519-verified data-check-string.
    launch_id: str


def _decode(component: str) -> str:
    if _BAD_PERCENT_RE.search(component):
        raise InitDataError("malformed")
    try:
        return unquote_plus(component, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        raise InitDataError("malformed") from None


def parse_fields(init_data: str) -> dict[str, str]:
    """Strict query-string parser: no blanks, no duplicates (also after decoding)."""
    fields: dict[str, str] = {}
    for part in init_data.split("&"):
        if "=" not in part:
            raise InitDataError("malformed")
        raw_key, raw_value = part.split("=", 1)
        key, value = _decode(raw_key), _decode(raw_value)
        if not _KEY_RE.match(key) or "\n" in value or "\r" in value:
            raise InitDataError("malformed")
        if key in fields:
            raise InitDataError("duplicate_field")
        fields[key] = value
    return fields


def compute_hash(fields: dict[str, str], bot_token: str) -> str:
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields) if k != "hash")
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()


def ed25519_check_string(fields: dict[str, str], bot_id: int) -> str:
    signed = "\n".join(f"{k}={fields[k]}" for k in sorted(fields) if k not in ("hash", "signature"))
    return f"{bot_id}:WebAppData\n{signed}"


def verify_ed25519(fields: dict[str, str], bot_id: int, public_key_hex: str) -> str:
    """Raises InitDataError unless `signature` is Telegram's Ed25519 signature of these fields for
    this bot; returns the launch id (SHA-256 of the verified data-check-string)."""
    raw = fields.get("signature")
    if raw is None:
        raise InitDataError("missing_signature")
    if not _SIGNATURE_RE.match(raw):
        raise InitDataError("bad_signature_format")
    try:
        signature = base64.urlsafe_b64decode(raw.rstrip("=") + "==")
    except (binascii.Error, ValueError):
        raise InitDataError("bad_signature_format") from None
    # Exactly one encoding per signature: the decoded bytes must re-encode to what was received.
    if len(signature) != 64 or base64.urlsafe_b64encode(signature).decode().rstrip("=") != raw.rstrip("="):
        raise InitDataError("bad_signature_format")
    check_string = ed25519_check_string(fields, bot_id).encode()
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(signature, check_string)
    except InvalidSignature:
        raise InitDataError("bad_signature") from None
    return hashlib.sha256(check_string).hexdigest()


def ed25519_shadow_status(init_data: str, bot_id: int, public_key_hex: str) -> str:
    """For an HMAC deployment measuring readiness for Ed25519: ok | missing | bad. Never raises."""
    try:
        verify_ed25519(parse_fields(init_data), bot_id, public_key_hex)
    except InitDataError as exc:
        return "missing" if exc.code == "missing_signature" else "bad"
    except Exception:  # noqa: BLE001 — a measurement must never break a login
        return "bad"
    return "ok"


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InitDataError("bad_user")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise InitDataError("bad_user")  # NaN / Infinity are not JSON


def _parse_user(raw: str | None) -> TelegramUser:
    if raw is None:
        raise InitDataError("missing_user")
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise InitDataError("bad_user") from None
    if not isinstance(data, dict):
        raise InitDataError("bad_user")
    user_id = data.get("id")
    # bool is a subclass of int; floats/strings are rejected outright.
    if type(user_id) is not int or not 0 < user_id <= MAX_TELEGRAM_ID:
        raise InitDataError("bad_user")
    for name in _STR_FIELDS:
        if name in data and not isinstance(data[name], str):
            raise InitDataError("bad_user")
    for name in _BOOL_FIELDS:
        if name in data and not isinstance(data[name], bool):
            raise InitDataError("bad_user")
    if data.get("is_bot") is True:
        raise InitDataError("bad_user")
    return TelegramUser(
        id=user_id,
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
        username=data.get("username"),
        language_code=data.get("language_code"),
    )


def validate_init_data(
    init_data: str,
    bot_token: str | None = None,
    *,
    now: float,
    max_age_s: int,
    future_skew_s: int,
    max_chars: int = 4096,
    ed25519: tuple[int, str] | None = None,
) -> VerifiedInitData:
    """Returns verified data or raises InitDataError. `now` is unix seconds.

    Exactly one of `bot_token` (HMAC) and `ed25519` = (bot_id, Telegram public key hex) is given."""
    if (bot_token is None) == (ed25519 is None):
        raise ValueError("exactly one of bot_token and ed25519 is required")
    if not isinstance(init_data, str) or not init_data:
        raise InitDataError("empty")
    if len(init_data) > max_chars:
        raise InitDataError("too_large")

    fields = parse_fields(init_data)
    if ed25519 is not None:
        launch_id = verify_ed25519(fields, *ed25519)
    else:
        received = fields.get("hash")
        if received is None or not _HASH_RE.match(received):
            raise InitDataError("bad_hash_format")
        if not hmac.compare_digest(compute_hash(fields, bot_token), received):  # type: ignore[arg-type]
            raise InitDataError("bad_signature")
        launch_id = received

    # Everything below is signed by Telegram; checks are about freshness and shape.
    raw_date = fields.get("auth_date")
    if raw_date is None:
        raise InitDataError("missing_auth_date")
    if not _AUTH_DATE_RE.match(raw_date):
        raise InitDataError("bad_auth_date")
    auth_date = int(raw_date)
    if auth_date - now > future_skew_s:
        raise InitDataError("from_future")
    if now - auth_date > max_age_s:
        raise InitDataError("expired")

    return VerifiedInitData(user=_parse_user(fields.get("user")), auth_date=auth_date, launch_id=launch_id)
