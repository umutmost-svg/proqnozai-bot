"""Server configuration for the Mini App API.

Nothing here runs at import time: settings are built explicitly by the app
factory (from the process environment) or by tests (directly). The bot's
own `.env` / `config.py` are never read.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

Environment = Literal["production", "development"]
SameSite = Literal["lax", "strict", "none"]

_LOOPBACK_HOSTS = {"localhost"}


class SettingsError(ValueError):
    """Invalid configuration. Messages never contain secret values."""


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_origin(raw: str) -> str:
    """Canonical `scheme://host[:port]`; rejects paths, wildcards and `null`."""
    value = raw.strip()
    if not value or value == "null" or "*" in value:
        raise SettingsError(f"invalid trusted origin: {value!r}")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SettingsError(f"invalid trusted origin: {value!r}")
    if parts.path not in ("",) or parts.query or parts.fragment or parts.username or parts.password:
        raise SettingsError(f"trusted origin must be scheme://host[:port] only: {value!r}")
    port = parts.port
    default_port = 443 if parts.scheme == "https" else 80
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default_port else "")


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    db_path: Path
    trusted_origins: frozenset[str]
    environment: Environment = "production"
    # Application policy, not Telegram requirements (see docs/miniapp/auth.md).
    init_data_max_age_s: int = 300
    init_data_future_skew_s: int = 30
    session_ttl_s: int = 12 * 3600
    max_body_bytes: int = 8 * 1024
    max_init_data_chars: int = 4096
    login_rate_limit: int = 10
    login_rate_window_s: int = 60
    cookie_samesite: SameSite = "lax"

    def __post_init__(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise SettingsError("bot token is missing or malformed")
        if self.db_path.name.startswith("bot.db"):
            raise SettingsError("the Mini App must not use the bot database (bot.db)")
        if not self.trusted_origins:
            raise SettingsError("at least one trusted origin is required")
        for name in ("init_data_max_age_s", "session_ttl_s", "max_body_bytes", "max_init_data_chars",
                     "login_rate_limit", "login_rate_window_s"):
            if getattr(self, name) <= 0:
                raise SettingsError(f"{name} must be positive")
        if self.init_data_future_skew_s < 0:
            raise SettingsError("init_data_future_skew_s must be >= 0")
        if self.environment not in ("production", "development"):
            raise SettingsError("environment must be 'production' or 'development'")
        if self.cookie_samesite not in ("lax", "strict", "none"):
            raise SettingsError("cookie_samesite must be lax, strict or none")
        for origin in self.trusted_origins:
            if normalize_origin(origin) != origin:
                raise SettingsError(f"trusted origin is not normalized: {origin!r}")
            parts = urlsplit(origin)
            if self.environment == "production" and parts.scheme != "https":
                raise SettingsError("production requires https trusted origins")
            if self.environment == "development" and not _is_loopback(parts.hostname or ""):
                # The only place where the Secure cookie flag is dropped: plain-HTTP loopback dev.
                raise SettingsError("development mode is limited to loopback origins")
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise SettingsError("SameSite=None requires Secure cookies (production)")

    @property
    def cookie_secure(self) -> bool:
        return self.environment == "production"

    @property
    def cookie_name(self) -> str:
        # `__Host-` makes browsers enforce Secure + Path=/ + no Domain (host-only).
        return "__Host-sporai_session" if self.cookie_secure else "sporai_session"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise SettingsError(f"{name} is required")
            return value

        def integer(name: str, default: int) -> int:
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                return int(raw)
            except ValueError:
                raise SettingsError(f"{name} must be an integer") from None

        origins = frozenset(
            normalize_origin(o) for o in required("MINIAPP_TRUSTED_ORIGINS").split(",") if o.strip()
        )
        return cls(
            bot_token=required("MINIAPP_BOT_TOKEN"),
            db_path=Path(required("MINIAPP_DB_PATH")),
            trusted_origins=origins,
            environment=env.get("MINIAPP_ENV", "production").strip() or "production",  # type: ignore[arg-type]
            init_data_max_age_s=integer("MINIAPP_INIT_DATA_MAX_AGE_S", 300),
            init_data_future_skew_s=integer("MINIAPP_INIT_DATA_FUTURE_SKEW_S", 30),
            session_ttl_s=integer("MINIAPP_SESSION_TTL_S", 12 * 3600),
            login_rate_limit=integer("MINIAPP_LOGIN_RATE_LIMIT", 10),
            login_rate_window_s=integer("MINIAPP_LOGIN_RATE_WINDOW_S", 60),
            cookie_samesite=env.get("MINIAPP_COOKIE_SAMESITE", "lax").strip().lower() or "lax",  # type: ignore[arg-type]
        )
