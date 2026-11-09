"""Operator CLI for the Mini App database. Runs inside the service (same env):

    python -m miniapp.admin stats
    python -m miniapp.admin revoke-user <telegram_user_id>
    python -m miniapp.admin revoke-all --yes
    python -m miniapp.admin purge
    python -m miniapp.admin backup <new-file>
    python -m miniapp.admin restore <backup-file> --yes
    python -m miniapp.admin forecasts

Safe by construction: settings come from the same MINIAPP_* env as the API
(same bot.db guard and bounds); a missing database is an error, never created;
output is counts only (no tokens, hashes or personal data). Writes use the same
IMMEDIATE transactions as the API, so it can run while the API is serving.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from miniapp.forecast.history import ForecastHistory, history_path
from miniapp.settings import Settings, SettingsError, is_bot_database_path
from miniapp.store import BurnWindow, ForeignDatabaseError, Store

logger = logging.getLogger("miniapp.admin")


def _finite(value: object) -> object:
    """NaN (e.g. the hit rate without any pick) as null: the output stays strict JSON."""
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


def _forecasts(settings: Settings) -> int:
    """Read-only: the Mini App database is not opened and the history is opened mode=ro (SQLite
    may still add its own -wal/-shm sidecar files next to a WAL database; no data changes)."""
    path = history_path(settings.db_path)
    if not path.is_file():
        print("forecast history file not found (no forecast stored yet?)", file=sys.stderr)
        return 2
    history = ForecastHistory(path, read_only=True)
    try:
        if not history.is_forecast_history():
            print("refused: the history file is not a forecast history", file=sys.stderr)
            return 2
        result = {"forecasts": history.status_counts(), "quality": _finite(history.evaluate())}
    except sqlite3.Error as exc:
        print(f"refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m miniapp.admin", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stats", help="counts of users, sessions and burned launches")
    revoke_user = sub.add_parser("revoke-user", help="end every session of one Telegram user")
    revoke_user.add_argument("telegram_user_id", type=int)
    revoke_all = sub.add_parser("revoke-all", help="emergency: end every session of every user")
    revoke_all.add_argument("--yes", action="store_true", help="confirm; without it nothing is changed")
    sub.add_parser("purge", help="delete long-ended sessions and expired burns")
    sub.add_parser("forecasts", help="forecast history: settlement counts and quality metrics (read-only)")
    backup = sub.add_parser("backup", help="consistent online copy into a NEW file")
    backup.add_argument("destination", type=Path)
    restore = sub.add_parser("restore", help="replace the live data with a backup, then revoke every session")
    restore.add_argument("source", type=Path)
    restore.add_argument("--yes", action="store_true", help="confirm; without it nothing is changed")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None,
         clock: Callable[[], float] = time.time) -> int:
    args = _parser().parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Files a root process creates (WAL, SHM, backups) would be unwritable for
        # the app user. In the container use: miniapp-entrypoint admin <command>.
        print("refused: do not run as root; use `miniapp-entrypoint admin <command>`", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_env(os.environ if env is None else env)
    except SettingsError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "forecasts":
        return _forecasts(settings)
    if not settings.db_path.is_file():
        # Never create an empty database by mistake (wrong path, volume not mounted).
        print("database file not found: check MINIAPP_DB_PATH and the volume mount", file=sys.stderr)
        return 2
    store = Store(settings.db_path)
    try:
        store.init_schema()
    except ForeignDatabaseError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    now = int(clock())
    burn = BurnWindow.for_policy(now, settings.init_data_max_age_s, settings.init_data_future_skew_s)
    result: dict[str, object]
    if args.command == "stats":
        result = dict(store.stats(now))
    elif args.command == "revoke-user":
        if args.telegram_user_id <= 0:
            print("telegram_user_id must be positive", file=sys.stderr)
            return 2
        result = {"revoked": store.revoke_user_sessions(args.telegram_user_id, now, burn)}
        logger.warning("admin.revoke_user user=%s revoked=%s", args.telegram_user_id, result["revoked"])
    elif args.command == "revoke-all":
        if not args.yes:
            print("revoke-all ends every session; repeat with --yes to confirm", file=sys.stderr)
            return 2
        result = {"revoked": store.revoke_all_sessions(now, burn)}
        logger.warning("admin.revoke_all revoked=%s", result["revoked"])
    elif args.command == "purge":
        store.purge_expired(now)
        result = dict(store.stats(now))
    elif args.command == "restore":
        if not args.yes:
            print("restore replaces all data and ends every session; repeat with --yes", file=sys.stderr)
            return 2
        if not args.source.is_file():
            print("refused: restore source not found", file=sys.stderr)
            return 2
        try:
            # One transaction: restored sessions are revoked before anyone can use them.
            revoked = store.restore_from(args.source, now, burn)
        except (ForeignDatabaseError, ValueError, sqlite3.Error) as exc:
            print(f"refused: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        result = {"restored": True, "revoked": revoked}
        logger.warning("admin.restore revoked=%s", result["revoked"])
    else:  # backup
        destination: Path = args.destination
        if is_bot_database_path(destination):
            print("refused: the destination looks like the bot database (bot.db)", file=sys.stderr)
            return 2
        try:
            store.backup(destination)
        except (FileExistsError, ValueError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        result = {"backup": str(destination)}
        logger.warning("admin.backup done")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sys.exit(main())
