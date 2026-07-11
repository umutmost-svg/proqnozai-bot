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
    tests independent without recreating the file."""
    import db
    db.db_init()
    return db


@pytest.fixture()
def clean_mostbet_cache():
    from config import mostbet_cache
    mostbet_cache.clear()
    yield mostbet_cache
    mostbet_cache.clear()
