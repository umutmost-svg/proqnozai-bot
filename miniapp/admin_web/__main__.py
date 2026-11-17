"""CLI of the admin panel.

Local pilot (ADMIN_WEB_MODE=local-pilot, the default):
    python -m miniapp.admin_web init     # pilot directory + demo data + three demo accounts
    python -m miniapp.admin_web serve    # http://127.0.0.1:8795 (loopback only)
    python -m miniapp.admin_web code <email>   # current 2FA code of a pilot account

Server (ADMIN_WEB_MODE=server, docs/ops/admin-server.md):
    python -m miniapp.admin_web serve                 # behind Cloudflare Access + the edge secret
    python -m miniapp.admin_web create-owner <email>  # the first owner, interactively (password asked twice,
                                                      # the 2FA secret shown once on that terminal only)
    python -m miniapp.admin_web unlock <email>        # end a 2FA lock early
    python -m miniapp.admin_web reset-credentials <email>   # lost password or 2FA device (interactive, like
                                                      # create-owner): new password + new 2FA secret, sessions end

Environment: ADMIN_PILOT_DIR (default ~/.sporbot-admin-pilot), ADMIN_WEB_PORT (default 8795).
Passwords and 2FA secrets go only to <pilot dir>/credentials.txt (0600), never to the repository
or the terminal log.
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
import time

from miniapp.admin_web import totp
from miniapp.admin_web.accounts import AdminStore
from miniapp.admin_web.config import AdminWebConfig, ConfigError

PILOT_ACCOUNTS = (("owner@sporbot.local", "owner"), ("admin@sporbot.local", "admin"),
                  ("analyst@sporbot.local", "analyst"))


def _init(config: AdminWebConfig) -> int:
    # Only a NEW (or empty) directory: an existing data directory of a stand or production is never
    # touched — no chmod, no accounts, no demo files.
    if config.data_dir.exists() and any(config.data_dir.iterdir()):
        print(f"refused: {config.data_dir} is not empty (choose a new ADMIN_PILOT_DIR or delete the old pilot)",
              file=sys.stderr)
        return 2
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config.data_dir, 0o700)
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
    from miniapp.admin_web.crm import CrmStore
    from miniapp.admin_web.demo import seed_crm
    CrmStore(accounts).init_schema()
    owner_id = next(a["id"] for a in accounts.admins() if a["role"] == "owner")
    with accounts._write() as conn:
        seed_crm(conn, owner_id, int(time.time()))
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
    # No access log: it would write every URL with its query (searches, user ids) to the container log.
    uvicorn.run(create_admin_app(config), host=config.bind, port=config.port, proxy_headers=False,
                server_header=False, log_level="info", access_log=False)
    return 0


MIN_OWNER_PASSWORD = 14


def _create_owner(config: AdminWebConfig, email: str) -> int:
    """Server mode: the first owner, typed by the owner on their own SSH terminal. Refused once any owner exists
    (more admins are invited from the panel); refused without a terminal, so the password never comes from a pipe
    or a log and the 2FA secret is never written to a file."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("refused: run it on an interactive terminal (docker exec -it ...)", file=sys.stderr)
        return 2
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        print("refused: not an e-mail address", file=sys.stderr)
        return 2
    accounts = AdminStore(config.admin_db)
    accounts.init_schema()
    if any(a["role"] == "owner" for a in accounts.admins()):
        print("refused: an owner already exists (invite others from the panel)", file=sys.stderr)
        return 2
    password = getpass.getpass(f"password for {email} (at least {MIN_OWNER_PASSWORD} characters): ")
    if len(password) < MIN_OWNER_PASSWORD or password != getpass.getpass("repeat: "):
        print("refused: too short or the two entries differ", file=sys.stderr)
        return 2
    _, secret = accounts.create_admin(email, "owner", password, int(time.time()))
    assert secret is not None
    print("\nOwner created. Add this account to your authenticator app NOW — it is shown only once:")
    print(f"  2FA secret: {secret}")
    print(f"  otpauth:    {totp.provisioning_uri(secret, email)}")
    print("Then sign in at the admin URL with the e-mail, the password and the 6-digit code.")
    return 0


def _unlock(config: AdminWebConfig, email: str) -> int:
    if not AdminStore(config.admin_db).cli_unlock(email, int(time.time())):
        print("no such account", file=sys.stderr)
        return 2
    print("unlocked")
    return 0


def _reset_credentials(config: AdminWebConfig, email: str) -> int:
    """Recovery for an active account (the only owner included): same terminal rules as create-owner."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("refused: run it on an interactive terminal (docker exec -it ...)", file=sys.stderr)
        return 2
    password = getpass.getpass(f"new password for {email} (at least {MIN_OWNER_PASSWORD} characters): ")
    if len(password) < MIN_OWNER_PASSWORD or len(password) > 200 or password != getpass.getpass("repeat: "):
        print("refused: too short or the two entries differ", file=sys.stderr)
        return 2
    secret = AdminStore(config.admin_db).cli_reset(email, password, int(time.time()))
    if secret is None:
        print("refused: no active account with this e-mail", file=sys.stderr)
        return 2
    print("\nDone. Add this NEW 2FA secret to your authenticator app now (the old one no longer works):")
    print(f"  2FA secret: {secret}")
    print(f"  otpauth:    {totp.provisioning_uri(secret, email.strip())}")
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
    sub.add_parser("init")
    sub.add_parser("serve")
    code = sub.add_parser("code")
    code.add_argument("email")
    owner = sub.add_parser("create-owner")
    owner.add_argument("email")
    for name in ("unlock", "reset-credentials"):
        sub.add_parser(name).add_argument("email")
    args = parser.parse_args(argv)
    try:
        config = AdminWebConfig.from_env(os.environ)
    except (ConfigError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if config.server and args.command in ("init", "code"):
        # No demo directory and no 2FA-code printing on a server: a code shown by the CLI would bypass the 2FA.
        print(f"refused: `{args.command}` is a local pilot command", file=sys.stderr)
        return 2
    if not config.server and args.command == "create-owner":
        print("refused: `create-owner` is a server command (the pilot creates its accounts with `init`)", file=sys.stderr)
        return 2
    if args.command == "init":
        return _init(config)
    try:
        config.require_data()
    except ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.command == "serve":
        return _serve(config)
    if args.command == "create-owner":
        return _create_owner(config, args.email)
    if args.command == "unlock":
        return _unlock(config, args.email)
    if args.command == "reset-credentials":
        return _reset_credentials(config, args.email)
    return _code(config, args.email)


if __name__ == "__main__":
    sys.exit(main())
