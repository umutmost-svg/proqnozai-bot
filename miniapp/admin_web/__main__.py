"""Local pilot CLI of the admin panel.

    python -m miniapp.admin_web init     # pilot directory + demo data + three demo accounts
    python -m miniapp.admin_web serve    # http://127.0.0.1:8795 (loopback only)
    python -m miniapp.admin_web code <email>   # current 2FA code of a pilot account

Environment: ADMIN_PILOT_DIR (default ~/.sporbot-admin-pilot), ADMIN_WEB_PORT (default 8795).
Passwords and 2FA secrets go only to <pilot dir>/credentials.txt (0600), never to the repository
or the terminal log.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import time

from miniapp.admin_web import totp
from miniapp.admin_web.accounts import AdminStore
from miniapp.admin_web.config import LOOPBACK, AdminWebConfig, ConfigError

PILOT_ACCOUNTS = (("owner@sporbot.local", "owner"), ("admin@sporbot.local", "admin"),
                  ("analyst@sporbot.local", "analyst"))


def _init(config: AdminWebConfig, empty: bool) -> int:
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config.data_dir, 0o700)
    if config.admin_db.exists():
        print(f"refused: {config.admin_db} exists (delete {config.data_dir} to start over)", file=sys.stderr)
        return 2
    if not empty:
        from miniapp.admin_web.demo import seed
        meta = seed(config.data_dir)
        print(f"demo data: {meta['users']} users, {meta['forecasts']} forecasts (dataset=demo)")
    accounts = AdminStore(config.admin_db)
    accounts.init_schema()
    lines = ["# SporBot admin LOCAL PILOT accounts. Demo only; delete the pilot directory when done.", ""]
    for email, role in PILOT_ACCOUNTS:
        password = secrets.token_urlsafe(12)
        _, secret = accounts.create_admin(email, role, password, int(time.time()))
        assert secret is not None
        lines += [f"{role}: {email}", f"  password: {password}", f"  2FA secret: {secret}",
                  f"  otpauth: {totp.provisioning_uri(secret, email)}", ""]
    path = config.data_dir / "credentials.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines))
    print(f"accounts: {', '.join(e for e, _ in PILOT_ACCOUNTS)}; passwords and 2FA secrets in {path} (0600)")
    return 0


def _serve(config: AdminWebConfig) -> int:
    import uvicorn

    from miniapp.admin_web.app import create_admin_app

    if config.static_dir is None:
        print("note: admin-frontend/dist not found; build it with `npm run build` in admin-frontend/",
              file=sys.stderr)
    uvicorn.run(create_admin_app(config), host=LOOPBACK, port=config.port, proxy_headers=False,
                server_header=False, log_level="info")
    return 0


def _code(config: AdminWebConfig, email: str) -> int:
    secret = AdminStore(config.admin_db).totp_secret_for_cli(email)
    if not secret:
        print("no such pilot account with 2FA", file=sys.stderr)
        return 2
    step = int(time.time() // totp.STEP_S)
    left = totp.STEP_S - int(time.time()) % totp.STEP_S
    print(f"{totp.code_at(secret, step)}  (valid ~{left} s more)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m miniapp.admin_web")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--empty", action="store_true", help="accounts only, no demo data")
    sub.add_parser("serve")
    code = sub.add_parser("code")
    code.add_argument("email")
    args = parser.parse_args(argv)
    try:
        config = AdminWebConfig.from_env(os.environ)
    except (ConfigError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.command == "init":
        return _init(config, args.empty)
    if args.command == "serve":
        return _serve(config)
    return _code(config, args.email)


if __name__ == "__main__":
    sys.exit(main())
