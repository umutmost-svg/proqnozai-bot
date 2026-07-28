"""Startup hygiene: noisy request logs are quieted (so the bot token never
reaches the logs) and the deploy-version banner resolves to something usable."""
import logging

import main


def test_request_loggers_are_quieted_to_warning():
    # httpx logs full request URLs (incl. the Telegram token) at INFO — must be
    # raised to WARNING by importing config.
    import config  # noqa: F401  (import applies the log-level side effect)
    for name in ("httpx", "httpcore", "telegram.ext.Updater"):
        assert logging.getLogger(name).level == logging.WARNING, name


def test_deploy_version_prefers_env_commit(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "deadbeefcafebabe0001")
    assert main._deploy_version() == "deadbeefcafe"   # trimmed to 12 chars


def test_deploy_version_is_nonempty_without_env(monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    v = main._deploy_version()
    assert isinstance(v, str) and v            # git short hash or "unknown"
