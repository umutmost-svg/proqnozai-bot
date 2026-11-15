"""ASGI entry point; settings come from MINIAPP_* environment variables only.

    uvicorn --factory miniapp.asgi:create_app_from_env --host 127.0.0.1 --port 8000 --no-proxy-headers

The API never uses the client address or X-Forwarded-*: TLS, the real client IP
and per-IP limits belong to the reverse proxy in front (docs/miniapp/auth.md).
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from miniapp.app import create_app
from miniapp.settings import Settings


def create_app_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    return create_app(Settings.from_env(os.environ))
