"""Configuration of the admin process. Only the LOCAL PILOT mode exists so far.

The pilot listens on the loopback interface only and reads an isolated data directory
(never the bot's bot.db, never a production or stand file). A server mode with
`__Host-` Secure cookies, an edge secret and Cloudflare Access is stage D2/D9 work and
deliberately has no switch here, so no pilot simplification can leak into it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from miniapp.forecast.history import history_path
from miniapp.settings import is_bot_database_path

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8795
DEFAULT_DIR = Path.home() / ".sporbot-admin-pilot"


class ConfigError(ValueError):
    pass


def _public_name(value: str | None, name: str) -> str | None:
    value = (value or "").strip().lstrip("@")
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", value):
        raise ConfigError(f"{name}: letters, digits and _ only")
    return value


@dataclass(frozen=True)
class AdminWebConfig:
    data_dir: Path
    port: int
    static_dir: Path | None
    # Public names for building referral links (optional; without them the panel shows the start parameter only).
    bot_username: str | None = None
    miniapp_short_name: str | None = None

    @property
    def miniapp_db(self) -> Path:
        return self.data_dir / "miniapp.db"

    @property
    def history_db(self) -> Path:
        return history_path(self.miniapp_db)

    @property
    def admin_db(self) -> Path:
        return self.data_dir / "admin" / "admin.db"

    @property
    def outbox_db(self) -> Path:
        """admin -> bot message queue: the admin writes jobs, the existing bot sends them (§10)."""
        return self.data_dir / "bot-outbox.db"

    @property
    def bot_crm_db(self) -> Path:
        """bot -> admin file (referral starts, user inquiries), shared with the bot (bot_crm.py)."""
        return self.data_dir / "bot-crm.db"

    @property
    def access_policy_file(self) -> Path:
        """Users blocked in the panel (Mini App access only), signed; read by the Mini App."""
        return self.data_dir / "access-policy.json"

    @property
    def telemetry_db(self) -> Path:
        """Mini App telemetry (activity, ad events): read-only for the admin (admin-dashboard.md §3)."""
        return self.data_dir / "miniapp.telemetry.db"

    @property
    def ads_policy_file(self) -> Path:
        """The published ads snapshot, next to the Mini App database (shared data directory, §2)."""
        return self.data_dir / "ads-policy.json"

    @property
    def ads_applied_file(self) -> Path:
        """Written by the Mini App: the snapshot version it applied last."""
        return self.data_dir / "ads-policy.applied"

    @property
    def labels_file(self) -> Path:
        return self.data_dir / "event_labels.json"

    @property
    def origins(self) -> frozenset[str]:
        return frozenset({f"http://{LOOPBACK}:{self.port}", f"http://localhost:{self.port}"})

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset({f"{LOOPBACK}:{self.port}", f"localhost:{self.port}"})

    def dataset(self) -> str:
        try:
            return str(json.loads((self.data_dir / "pilot.json").read_text())["dataset"])
        except (OSError, ValueError, KeyError):
            return "local"

    def require_pilot_data(self) -> None:
        """Serve only a directory created by `init` with the demo set: never a stand/production data dir."""
        if self.dataset() != "demo":
            raise ConfigError(f"{self.data_dir} is not a pilot directory created by `init` (pilot.json missing)")
        mode = self.data_dir.stat().st_mode & 0o777
        if mode & 0o077:
            raise ConfigError(f"{self.data_dir} must be private (0700), found {oct(mode)}")

    def validate(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        if self.data_dir.expanduser().resolve().is_relative_to(repo):
            raise ConfigError("the pilot directory holds passwords and 2FA secrets: it must be outside the repository")
        if not 1024 <= self.port <= 65535:
            raise ConfigError("port must be 1024..65535")
        if is_bot_database_path(self.miniapp_db):
            raise ConfigError("the pilot never opens the bot database")

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ) -> AdminWebConfig:
        if env.get("ADMIN_WEB_MODE", "local-pilot") != "local-pilot":
            raise ConfigError("only ADMIN_WEB_MODE=local-pilot is implemented")
        static = env.get("ADMIN_WEB_STATIC_DIR")
        default_static = Path(__file__).resolve().parents[2] / "admin-frontend" / "dist"
        config = cls(
            data_dir=Path(env.get("ADMIN_PILOT_DIR") or DEFAULT_DIR).expanduser(),
            port=int(env.get("ADMIN_WEB_PORT", DEFAULT_PORT)),
            static_dir=Path(static) if static else (default_static if default_static.is_dir() else None),
            bot_username=_public_name(env.get("ADMIN_BOT_USERNAME"), "ADMIN_BOT_USERNAME"),
            miniapp_short_name=_public_name(env.get("ADMIN_MINIAPP_SHORT_NAME"), "ADMIN_MINIAPP_SHORT_NAME"),
        )
        config.validate()
        return config
