"""Configuration of the admin process: the LOCAL PILOT or the SERVER mode.

local-pilot: loopback only, an isolated demo data directory (never the bot's bot.db, never a production or stand
file), demo telemetry on request, a CLI that prints 2FA codes.

server (ADMIN_WEB_MODE=server): behind Cloudflare Access + Tunnel on one public host (ADMIN_PUBLIC_HOST), HTTPS only.
Every request must carry the edge secret (ADMIN_EDGE_SECRET in EDGE_SECRET_HEADER, injected by the edge), so a
request reaching the process by any other route (the server's public :80 with a forged Host) is refused. Cookies are
`__Host-`/`__Secure-` Secure; the data directory is the Mini App's own (its miniapp.db must exist), the admin's own
accounts in ADMIN_STATE_DIR (a separate volume); no demo data, no 2FA-code CLI. Nothing of the pilot's simplifications applies.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from miniapp.forecast.history import history_path
from miniapp.settings import is_bot_database_path

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8795
MODES = ("local-pilot", "server")
EDGE_SECRET_HEADER = "X-Admin-Edge-Auth"
MIN_EDGE_SECRET_CHARS = 32
_HOST = re.compile(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
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
    mode: str = "local-pilot"
    public_host: str | None = None  # server mode: the one host served (e.g. admin.proqnozai.com)
    edge_secret: str | None = field(default=None, repr=False)  # server mode: required on every request
    bind: str = LOOPBACK
    # Server mode: the admin's own accounts, sessions and 2FA secrets live on their OWN volume, never on the Mini
    # App's (the Mini App's process must not be able to read them).
    state_dir: Path | None = None

    @property
    def server(self) -> bool:
        return self.mode == "server"

    @property
    def session_cookie(self) -> str:
        return "__Host-sporbot_admin_session" if self.server else "sporbot_admin_session"

    @property
    def pre_cookie(self) -> str:
        # Path-scoped (/api/admin/auth), so `__Secure-` (a `__Host-` cookie must have Path=/).
        return "__Secure-sporbot_admin_pre" if self.server else "sporbot_admin_pre"

    @property
    def miniapp_db(self) -> Path:
        return self.data_dir / "miniapp.db"

    @property
    def history_db(self) -> Path:
        return history_path(self.miniapp_db)

    @property
    def admin_dir(self) -> Path:
        return self.state_dir if self.server and self.state_dir is not None else self.data_dir / "admin"

    @property
    def admin_db(self) -> Path:
        return self.admin_dir / "admin.db"

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
        if self.server:
            return frozenset({f"https://{self.public_host}"})
        return frozenset({f"http://{LOOPBACK}:{self.port}", f"http://localhost:{self.port}"})

    @property
    def hosts(self) -> frozenset[str]:
        if self.server:
            return frozenset({self.public_host or ""})
        return frozenset({f"{LOOPBACK}:{self.port}", f"localhost:{self.port}"})

    def dataset(self) -> str:
        if self.server:
            return "production"
        try:
            return str(json.loads((self.data_dir / "pilot.json").read_text())["dataset"])
        except (OSError, ValueError, KeyError):
            return "local"

    def require_data(self) -> None:
        """The pilot serves only its demo directory; the server only an existing Mini App data directory."""
        if self.server:
            self.require_server_data()
        else:
            self.require_pilot_data()

    def require_server_data(self) -> None:
        if not self.miniapp_db.is_file():
            raise ConfigError(f"{self.miniapp_db} not found: the server mode reads the Mini App's own data directory")
        if (self.data_dir / "pilot.json").exists():
            raise ConfigError(f"{self.data_dir} is a pilot directory: the server mode never serves demo data")
        if self.admin_dir.resolve().is_relative_to(self.data_dir.resolve()):
            raise ConfigError("ADMIN_STATE_DIR must be outside the Mini App's data directory (its own volume)")
        self.admin_dir.mkdir(mode=0o700, exist_ok=True)
        mode = self.admin_dir.stat().st_mode & 0o777
        if mode & 0o077:
            raise ConfigError(f"{self.admin_dir} holds passwords and 2FA secrets: it must be private (0700), found {oct(mode)}")

    def require_pilot_data(self) -> None:
        """Serve only a directory created by `init` with the demo set: never a stand/production data dir."""
        if self.dataset() != "demo":
            raise ConfigError(f"{self.data_dir} is not a pilot directory created by `init` (pilot.json missing)")
        mode = self.data_dir.stat().st_mode & 0o777
        if mode & 0o077:
            raise ConfigError(f"{self.data_dir} must be private (0700), found {oct(mode)}")

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ConfigError(f"ADMIN_WEB_MODE must be one of {', '.join(MODES)}")
        if self.server:
            if not self.public_host or not _HOST.fullmatch(self.public_host):
                raise ConfigError("ADMIN_PUBLIC_HOST: a lowercase host name (e.g. admin.example.com) is required")
            secret = self.edge_secret or ""
            if len(secret) < MIN_EDGE_SECRET_CHARS or not secret.isascii() or not secret.isprintable() or " " in secret:
                raise ConfigError(f"ADMIN_EDGE_SECRET: at least {MIN_EDGE_SECRET_CHARS} printable characters, no spaces")
        elif self.bind != LOOPBACK:
            raise ConfigError("the local pilot listens on the loopback interface only")
        repo = Path(__file__).resolve().parents[2]
        if self.data_dir.expanduser().resolve().is_relative_to(repo):
            raise ConfigError("the pilot directory holds passwords and 2FA secrets: it must be outside the repository")
        if not 1024 <= self.port <= 65535:
            raise ConfigError("port must be 1024..65535")
        if is_bot_database_path(self.miniapp_db):
            raise ConfigError("the pilot never opens the bot database")

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ) -> AdminWebConfig:
        mode = env.get("ADMIN_WEB_MODE", "local-pilot")
        server = mode == "server"
        static = env.get("ADMIN_WEB_STATIC_DIR")
        default_static = Path(__file__).resolve().parents[2] / "admin-frontend" / "dist"
        if server and not (env.get("ADMIN_DATA_DIR") and env.get("ADMIN_STATE_DIR")):
            raise ConfigError("ADMIN_DATA_DIR (the Mini App's data) and ADMIN_STATE_DIR (the admin's own volume) "
                              "are required in the server mode")
        config = cls(
            mode=mode,
            public_host=(env.get("ADMIN_PUBLIC_HOST") or "").strip() or None,
            edge_secret=env.get("ADMIN_EDGE_SECRET") or None,
            # The server runs in a container: it listens on all of the container's interfaces (Traefik reaches it on
            # the Docker network; the edge secret, not the bind address, keeps other routes out).
            bind="0.0.0.0" if server else LOOPBACK,
            state_dir=Path(env["ADMIN_STATE_DIR"]) if server else None,
            data_dir=Path(env["ADMIN_DATA_DIR"] if server else (env.get("ADMIN_PILOT_DIR") or DEFAULT_DIR)).expanduser(),
            port=int(env.get("ADMIN_WEB_PORT", DEFAULT_PORT)),
            static_dir=Path(static) if static else (default_static if default_static.is_dir() else None),
            bot_username=_public_name(env.get("ADMIN_BOT_USERNAME"), "ADMIN_BOT_USERNAME"),
            miniapp_short_name=_public_name(env.get("ADMIN_MINIAPP_SHORT_NAME"), "ADMIN_MINIAPP_SHORT_NAME"),
        )
        config.validate()
        return config
