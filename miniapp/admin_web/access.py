"""Mini App access blocks published from the admin panel (admin-dashboard.md §10): `access-policy.json`.

The source of truth is `crm_blocks` in admin.db (a block and its journal event share one transaction, crm.py).
This module publishes the set of blocked Telegram ids as a snapshot signed with the admin's Ed25519 key
(the same key as ads-policy.json); the Mini App verifies it (security.AccessPolicy) and refuses sign-in and
sessions of listed users. A block ends Mini App access only: no user data is deleted anywhere.

Publication mirrors the ads snapshot: payload stored in `access_publications`, file written after the commit
under a lock, repaired whenever the file is missing, invalid, older or differs; versions only grow
(max(now·1000, last + 1, verified file + 1)).
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from miniapp.ads import policy
from miniapp.admin_web.accounts import AdminStore
from miniapp.admin_web.ads import load_signing_key
from miniapp.security import AccessPolicyError, parse_access_policy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_publications (
    version      INTEGER PRIMARY KEY,
    payload      TEXT NOT NULL,
    blocked      INTEGER NOT NULL,
    published_at INTEGER NOT NULL
);
"""


class AccessPublisher:
    def __init__(self, accounts: AdminStore, path: Path) -> None:
        self.accounts = accounts
        self.path = path
        admin_dir = accounts.path.parent
        self.signing_key = load_signing_key(admin_dir / "ads-signing.key")
        self.public_key = self.signing_key.public_key()
        self.lock_file = admin_dir / "access-publish.lock"
        self._thread_lock = threading.Lock()

    def init_schema(self) -> None:
        with self.accounts._connect() as conn:
            conn.executescript(_SCHEMA)

    def _on_file(self) -> tuple[int, frozenset[int]] | None:
        try:
            return parse_access_policy(self.path.read_bytes(), self.public_key)
        except (OSError, AccessPolicyError):
            return None

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        with self._thread_lock:
            fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def sync(self, now: int) -> dict:
        """Publishes a new version when the blocked set differs from the latest publication, then makes sure the
        file holds the latest one. Safe to call any time (startup, after a block/unblock, status reads)."""
        with self._file_lock():
            with self.accounts._write() as conn:
                blocked = sorted(r[0] for r in conn.execute("SELECT user_id FROM crm_blocks"))
                row = conn.execute("SELECT version, payload FROM access_publications ORDER BY version DESC "
                                   "LIMIT 1").fetchone()
                if row is None or json.loads(row[1])["blocked"] != blocked:
                    on_file = self._on_file()
                    version = max(now * 1000, (row[0] + 1) if row else 0, (on_file[0] + 1) if on_file else 0)
                    body = {"schema": 1, "kind": "access", "version": version, "generatedAt": now, "blocked": blocked}
                    payload = policy.canonical(policy.sign(body, self.signing_key))
                    conn.execute("INSERT INTO access_publications (version, payload, blocked, published_at) "
                                 "VALUES (?, ?, ?, ?)", (version, payload.decode(), len(blocked), now))
                    row = (version, payload.decode())
            on_file = self._on_file()
            written = True
            if on_file is None or on_file[0] < row[0] or (on_file[0] == row[0] and self.path.read_bytes() != row[1].encode()):
                try:
                    policy.write_atomic(self.path, row[1].encode())
                except OSError:
                    written = False
            else:
                try:
                    policy.sync_dir(self.path.parent)
                except OSError:
                    written = False
        return {"version": row[0], "blocked": len(blocked), "fileWritten": written}
