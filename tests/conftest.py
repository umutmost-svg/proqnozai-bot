"""Offline test bootstrap.

Controlled environment BEFORE any project import:
- required env vars get dummy values (config.py raises on missing ones —
  production validation stays intact, we just satisfy it up front);
- BOT_DB_DIR points at a session temp dir so the production bot.db is
  never touched;
- CWD is moved to that temp dir so config.py's log files (bot.log,
  suspicious.log) are created there, not in the repo.

No test in this package may hit the network: Telegram, Anthropic, Mostbet
and football APIs are out of bounds; HTTP is mocked where needed.
"""
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="proqnozai-tests-")
os.environ.setdefault("TELEGRAM_TOKEN", "0:offline-test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-offline-test")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("DASHBOARD_TOKEN", "offline-test-dashboard-token")
os.environ["BOT_DB_DIR"] = _tmp_dir
os.chdir(_tmp_dir)

import pytest  # noqa: E402


@pytest.fixture()
def temp_db():
    """Initialised schema in the session temp DB; unique uids per test keep
    tests independent without recreating the file. Also clears
    config.demand_cache — it's in-memory state derived from the DB, keyed
    only by the `days` window (not by uid), so it would otherwise leak a
    stale demand snapshot across tests that share the same window."""
    import db
    from config import demand_cache, winrate_cache
    db.db_init()
    demand_cache.clear()
    winrate_cache.clear()
    return db


@pytest.fixture()
def partners(temp_db):
    """Exclusive control of the `partners` table for one test.

    Partners are DB-backed now, and the temp DB is shared across the session,
    so a test that cares about the exact partner list has to start from an empty
    table and leave it empty. Call the fixture to seed: partners([("A", url)]).
    """
    def _clear():
        with temp_db.con() as c:
            c.execute("DELETE FROM partners")

    def _seed(entries=()):
        _clear()
        for name, url in entries:
            temp_db.db_partner_add(name, url)
        return temp_db.db_active_partners()

    _clear()
    yield _seed
    _clear()


@pytest.fixture()
def clean_mostbet_cache():
    from config import mostbet_cache
    mostbet_cache.clear()
    yield mostbet_cache
    mostbet_cache.clear()
