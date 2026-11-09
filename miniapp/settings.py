"""Server configuration for the Mini App API.

Nothing here runs at import time: settings are built explicitly by the app
factory (from the process environment) or by tests (directly). The bot's
own `.env` / `config.py` are never read.
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

# Every free-plan club competition with a current season (EC and WC: add them when they run).
DEFAULT_COMPETITIONS = "PL,PD,SA,BL1,FL1,CL,ELC,DED,PPL,BSA"

Environment = Literal["production", "development"]
SameSite = Literal["lax", "strict", "none"]
CspMode = Literal["enforce", "report-only"]

_LOOPBACK_HOSTS = {"localhost"}

# Upper bounds: a typo in env must stop the start, not quietly widen the replay window.
MAX_INIT_DATA_AGE_S = 3600
MAX_INIT_DATA_FUTURE_SKEW_S = 300
MAX_SESSION_TTL_S = 7 * 86400
MAX_SESSIONS_PER_USER = 5
# Shared secret between the edge (e.g. Cloudflare) and the app: long enough to be unguessable.
MIN_EDGE_SECRET_CHARS = 32


class SettingsError(ValueError):
    """Invalid configuration. Messages never contain secret values."""


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_bot_database_path(path: Path) -> bool:
    """True if the path, or the file it really points to, is the bot's `bot.db*`.

    `resolve()` follows symlinks (of the file and of its parent directories);
    names are compared case-insensitively because macOS file systems usually are.
    Hard links and copies under another name are caught by Store.init_schema,
    which refuses a file with foreign tables.
    """
    names = {path.name, path.expanduser().resolve().name}
    return any(name.casefold().startswith("bot.db") for name in names)


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
    # Successful-signature logins per verified Telegram user per window.
    login_rate_limit: int = 10
    login_rate_window_s: int = 60
    cookie_samesite: SameSite = "lax"
    # Active sessions per user; creating one more revokes the oldest.
    max_sessions_per_user: int = 5
    # Emergency switch: False rejects new logins, existing sessions keep working.
    logins_enabled: bool = True
    # Built frontend (index.html + assets) served on the same origin as /api; None = API only.
    static_dir: Path | None = None
    csp_mode: CspMode = "enforce"
    # If set, every request except GET /api/health must carry it in EDGE_SECRET_HEADER:
    # only the edge that injects it can reach the app (no bypass via another route).
    edge_secret: str | None = field(default=None, repr=False)
    # Sports data (M3). Without a key the sport is reported "not connected"; no demo data.
    football_data_key: str | None = field(default=None, repr=False)
    football_data_competitions: tuple[str, ...] = tuple(DEFAULT_COMPETITIONS.split(","))
    # True only on a paid plan with live scores; the free plan delays scores.
    football_data_live_plan: bool = False
    # Self-imposed budgets below the provider's own limits (free tier: 10 requests/minute).
    football_data_per_minute: int = 8
    football_data_per_day: int = 3000
    # Event ids never seen in a listing may cost an upstream call: capped per hour.
    sports_unknown_lookups_per_hour: int = 20
    # Odds pushed by the bot (HMAC-signed, POST /api/internal/odds). Unset: the route is off
    # and no odds are shown anywhere.
    odds_feed_secret: str | None = field(default=None, repr=False)
    # Kill switch: false answers signed pushes with 410 (the bot then asks the source for no
    # prices) and shows no odds anywhere, without touching the secret or the bot.
    odds_feed_enabled: bool = True
    # AI analysis (M3.2). Without a key the analysis is reported "not connected".
    anthropic_api_key: str | None = field(default=None, repr=False)
    ai_model: str = "claude-opus-5-5"
    # Model calls (cache hits are free): per user per UTC day, and for everyone.
    ai_per_user_daily: int = 10
    ai_daily_budget: int = 200

    def __post_init__(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise SettingsError("bot token is missing or malformed")
        if is_bot_database_path(self.db_path):
            raise SettingsError("the Mini App must not use the bot database (bot.db)")
        if not self.trusted_origins:
            raise SettingsError("at least one trusted origin is required")
        for name in ("init_data_max_age_s", "session_ttl_s", "max_body_bytes", "max_init_data_chars",
                     "login_rate_limit", "login_rate_window_s", "max_sessions_per_user"):
            if getattr(self, name) <= 0:
                raise SettingsError(f"{name} must be positive")
        if self.init_data_future_skew_s < 0:
            raise SettingsError("init_data_future_skew_s must be >= 0")
        for name, limit in (("init_data_max_age_s", MAX_INIT_DATA_AGE_S),
                            ("init_data_future_skew_s", MAX_INIT_DATA_FUTURE_SKEW_S),
                            ("session_ttl_s", MAX_SESSION_TTL_S),
                            ("max_sessions_per_user", MAX_SESSIONS_PER_USER)):
            if getattr(self, name) > limit:
                raise SettingsError(f"{name} must be <= {limit}")
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
        if self.csp_mode not in ("enforce", "report-only"):
            raise SettingsError("csp_mode must be enforce or report-only")
        if self.static_dir is not None and not (self.static_dir / "index.html").is_file():
            raise SettingsError("static_dir must contain index.html (build the frontend first)")
        if self.edge_secret is not None and (
            len(self.edge_secret) < MIN_EDGE_SECRET_CHARS or not self.edge_secret.isascii()
            or not self.edge_secret.isprintable() or " " in self.edge_secret
        ):
            raise SettingsError(f"edge secret must be >= {MIN_EDGE_SECRET_CHARS} printable ASCII chars without spaces")
        if self.odds_feed_secret is not None and (
            len(self.odds_feed_secret) < MIN_EDGE_SECRET_CHARS or not self.odds_feed_secret.isascii()
            or not self.odds_feed_secret.isprintable() or " " in self.odds_feed_secret
        ):
            raise SettingsError(f"odds feed secret must be >= {MIN_EDGE_SECRET_CHARS} printable ASCII chars without spaces")
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise SettingsError("SameSite=None requires Secure cookies (production)")
        if self.football_data_key is not None and (
            not self.football_data_key.isascii() or not self.football_data_key.isalnum()
            or not 16 <= len(self.football_data_key) <= 128
        ):
            raise SettingsError("football-data.org key must be 16-128 alphanumeric ASCII chars")
        from .sports.football_data import KNOWN_COMPETITIONS  # local: settings stay importable alone

        unknown = [c for c in self.football_data_competitions if c not in KNOWN_COMPETITIONS]
        if unknown or not self.football_data_competitions:
            raise SettingsError(f"unsupported football-data.org competitions: {unknown or 'none'}")
        # Free tier: 10/minute. A paid plan (declared by football_data_live_plan) allows more;
        # the value must still stay below that plan's own limit (set by the operator).
        max_per_minute = 120 if self.football_data_live_plan else 10
        if not 1 <= self.football_data_per_minute <= max_per_minute:
            raise SettingsError(f"football_data_per_minute must be 1..{max_per_minute} "
                                "(free tier: 10/minute; more only with MINIAPP_FOOTBALL_DATA_LIVE_PLAN)")
        if not 1 <= self.football_data_per_day <= 14400:
            raise SettingsError("football_data_per_day must be 1..14400")
        if not 0 <= self.sports_unknown_lookups_per_hour <= 600:
            raise SettingsError("sports_unknown_lookups_per_hour must be 0..600")
        if self.anthropic_api_key is not None and (
            not self.anthropic_api_key.isascii() or not self.anthropic_api_key.isprintable()
            or " " in self.anthropic_api_key or not 20 <= len(self.anthropic_api_key) <= 256
        ):
            raise SettingsError("Anthropic API key must be 20-256 printable ASCII chars without spaces")
        if not re.fullmatch(r"claude-[a-z0-9-]{1,60}", self.ai_model):
            raise SettingsError("ai_model must be a Claude model id such as claude-opus-5-5")
        if not 1 <= self.ai_per_user_daily <= 1000:
            raise SettingsError("ai_per_user_daily must be 1..1000")
        if not 1 <= self.ai_daily_budget <= 100000:
            raise SettingsError("ai_daily_budget must be 1..100000")

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

        def boolean(name: str, default: bool) -> bool:
            raw = (env.get(name) or "").strip().lower()
            if raw == "":
                return default
            if raw not in ("true", "false"):
                raise SettingsError(f"{name} must be true or false")
            return raw == "true"

        def optional(name: str) -> str | None:
            return (env.get(name) or "").strip() or None

        static_dir = optional("MINIAPP_STATIC_DIR")
        environment = env.get("MINIAPP_ENV", "production").strip() or "production"
        edge_secret = optional("MINIAPP_EDGE_SECRET")
        if environment == "production" and edge_secret is None:
            # A deployment is configured only through env: a forgotten secret must
            # stop the start, not silently open a route around the edge's limits.
            raise SettingsError("MINIAPP_EDGE_SECRET is required in production")
        origins = frozenset(
            normalize_origin(o) for o in required("MINIAPP_TRUSTED_ORIGINS").split(",") if o.strip()
        )
        return cls(
            bot_token=required("MINIAPP_BOT_TOKEN"),
            db_path=Path(required("MINIAPP_DB_PATH")),
            trusted_origins=origins,
            environment=environment,  # type: ignore[arg-type]
            init_data_max_age_s=integer("MINIAPP_INIT_DATA_MAX_AGE_S", 300),
            init_data_future_skew_s=integer("MINIAPP_INIT_DATA_FUTURE_SKEW_S", 30),
            session_ttl_s=integer("MINIAPP_SESSION_TTL_S", 12 * 3600),
            login_rate_limit=integer("MINIAPP_LOGIN_RATE_LIMIT", 10),
            login_rate_window_s=integer("MINIAPP_LOGIN_RATE_WINDOW_S", 60),
            cookie_samesite=env.get("MINIAPP_COOKIE_SAMESITE", "lax").strip().lower() or "lax",  # type: ignore[arg-type]
            max_sessions_per_user=integer("MINIAPP_MAX_SESSIONS_PER_USER", 5),
            logins_enabled=boolean("MINIAPP_LOGINS_ENABLED", True),
            static_dir=Path(static_dir) if static_dir else None,
            csp_mode=(optional("MINIAPP_CSP_MODE") or "enforce").lower(),  # type: ignore[arg-type]
            edge_secret=edge_secret,
            football_data_key=optional("MINIAPP_FOOTBALL_DATA_KEY"),
            football_data_competitions=tuple(
                c.strip().upper()
                for c in (optional("MINIAPP_FOOTBALL_DATA_COMPETITIONS") or DEFAULT_COMPETITIONS).split(",")
                if c.strip()
            ),
            football_data_live_plan=boolean("MINIAPP_FOOTBALL_DATA_LIVE_PLAN", False),
            football_data_per_minute=integer("MINIAPP_FOOTBALL_DATA_PER_MINUTE", 8),
            football_data_per_day=integer("MINIAPP_FOOTBALL_DATA_PER_DAY", 3000),
            sports_unknown_lookups_per_hour=integer("MINIAPP_SPORTS_UNKNOWN_LOOKUPS_PER_HOUR", 20),
            odds_feed_secret=optional("MINIAPP_ODDS_FEED_SECRET"),
            odds_feed_enabled=boolean("MINIAPP_ODDS_FEED_ENABLED", True),
            anthropic_api_key=optional("MINIAPP_ANTHROPIC_API_KEY"),
            ai_model=optional("MINIAPP_AI_MODEL") or "claude-opus-5-5",
            ai_per_user_daily=integer("MINIAPP_AI_PER_USER_DAILY", 10),
            ai_daily_budget=integer("MINIAPP_AI_DAILY_BUDGET", 200),
        )
