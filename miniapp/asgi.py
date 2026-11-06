"""ASGI entry point; settings come from MINIAPP_* environment variables only.

    uvicorn --factory miniapp.asgi:create_app_from_env --no-proxy-headers
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
