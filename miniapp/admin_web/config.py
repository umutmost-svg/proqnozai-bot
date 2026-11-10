"""Configuration of the admin process. Only the LOCAL PILOT mode exists so far.

The pilot listens on the loopback interface only and reads an isolated data directory
(never the bot's bot.db, never a production or stand file). A server mode with
`__Host-` Secure cookies, an edge secret and Cloudflare Access is stage D2/D9 work and
deliberately has no switch here, so no pilot simplification can leak into it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from miniapp.forecast.history import history_path
from miniapp.settings import is_bot_database_path

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8795
DEFAULT_DIR = Path.home() / ".sporbot-admin-pilot"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AdminWebConfig:
    data_dir: Path
    port: int
    static_dir: Path | None

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
        )
        config.validate()
        return config
