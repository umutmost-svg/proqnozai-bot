from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from miniapp.settings import Settings, SettingsError, normalize_origin
from miniapp.store import Store
from miniapp.telegram_auth import TelegramUser
from miniapp.tests.signing import TEST_BOT_TOKEN

REPO_ROOT = Path(__file__).resolve().parents[2]


def base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "MINIAPP_BOT_TOKEN": TEST_BOT_TOKEN,
        "MINIAPP_DB_PATH": str(tmp_path / "m.db"),
        "MINIAPP_TRUSTED_ORIGINS": "https://App.Example.com:443, https://other.example.com:8443",
        **extra,
    }


def test_from_env_defaults(tmp_path: Path) -> None:
    s = Settings.from_env(base_env(tmp_path))
    assert s.environment == "production" and s.cookie_secure
    assert s.trusted_origins == {"https://app.example.com", "https://other.example.com:8443"}
    assert (s.init_data_max_age_s, s.init_data_future_skew_s) == (300, 30)
    assert s.cookie_name == "__Host-sporai_session"
    assert TEST_BOT_TOKEN not in repr(s)


@pytest.mark.parametrize(
    "extra",
    [
        {"MINIAPP_BOT_TOKEN": ""},
        {"MINIAPP_TRUSTED_ORIGINS": "*"},
        {"MINIAPP_TRUSTED_ORIGINS": "null"},
        {"MINIAPP_TRUSTED_ORIGINS": "https://a.example.com/path"},
        {"MINIAPP_TRUSTED_ORIGINS": "http://a.example.com"},  # production needs https
        {"MINIAPP_ENV": "development", "MINIAPP_TRUSTED_ORIGINS": "http://192.168.1.5:5173"},
        {"MINIAPP_ENV": "staging"},
        {"MINIAPP_COOKIE_SAMESITE": "whatever"},
        {"MINIAPP_ENV": "development", "MINIAPP_TRUSTED_ORIGINS": "http://127.0.0.1:5173",
         "MINIAPP_COOKIE_SAMESITE": "none"},
        {"MINIAPP_SESSION_TTL_S": "0"},
        {"MINIAPP_INIT_DATA_MAX_AGE_S": "five"},
        {"MINIAPP_DB_PATH": "/data/bot.db"},
    ],
)
def test_invalid_settings(tmp_path: Path, extra: dict[str, str]) -> None:
    with pytest.raises(SettingsError) as exc:
        Settings.from_env(base_env(tmp_path, **extra))
    assert TEST_BOT_TOKEN not in str(exc.value)


def test_development_on_loopback(tmp_path: Path) -> None:
    s = Settings.from_env(base_env(tmp_path, MINIAPP_ENV="development",
                                   MINIAPP_TRUSTED_ORIGINS="http://127.0.0.1:5173,http://localhost:5173"))
    assert not s.cookie_secure and s.cookie_name == "sporai_session"


def test_normalize_origin() -> None:
    assert normalize_origin("HTTPS://Example.COM") == "https://example.com"
    assert normalize_origin("http://127.0.0.1:80") == "http://127.0.0.1"


def test_importing_package_has_no_side_effects(tmp_path: Path) -> None:
    # Fresh interpreter, empty env, empty cwd: importing must not need env,
    # create databases or import the bot's modules.
    code = (
        "import sys, miniapp.app, miniapp.asgi, miniapp.store, miniapp.settings, miniapp.telegram_auth;"
        "bad = [m for m in ('config', 'db', 'handlers', 'telegram', 'main') if m in sys.modules];"
        "assert not bad, bad"
    )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)}
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, check=True)
    assert list(tmp_path.iterdir()) == []


def test_package_does_not_import_bot_modules() -> None:
    for path in (REPO_ROOT / "miniapp").glob("*.py"):
        text = path.read_text()
        for name in ("config", "db", "handlers", "translations"):
            assert f"import {name}" not in text and f"from {name} " not in text, path


def test_upsert_is_atomic_under_concurrency(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.db")
    store.init_schema()
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            store.upsert_user(TelegramUser(77, f"n{i}", None, None, "ru"), now=1000 + i)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.count_users() == 1


def test_store_keeps_only_token_hash(tmp_path: Path) -> None:
    store = Store(tmp_path / "h.db")
    store.init_schema()
    store.upsert_user(TelegramUser(5, "x", None, None, None), now=1)
    token = "raw-session-token-value-for-test"
    store.create_session(token, 5, "a" * 64, now=1, ttl_s=10)
    raw = (tmp_path / "h.db").read_bytes() + b"".join(
        p.read_bytes() for p in tmp_path.glob("h.db-*")
    )
    assert token.encode() not in raw
    assert store.get_session(token) is not None


def test_tests_never_touch_bot_db() -> None:
    importlib.import_module("miniapp.store")
    assert not (Path.cwd() / "miniapp.db").exists()
