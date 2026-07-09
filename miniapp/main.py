"""ProqnozAI Mini App API — entrypoint (Stage 0: auth + health only).

Run: uvicorn main:app --host 0.0.0.0 --port $PORT   (from inside miniapp/)
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import db
from auth import validate_init_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info("Mini App API started")
    yield


app = FastAPI(title="ProqnozAI Mini App API", lifespan=lifespan)


def _authed_user(authorization: str | None) -> tuple[int, dict]:
    """Validate the 'Authorization: tma <initData>' header. Raises 401 on failure."""
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="missing initData")
    user = validate_init_data(authorization[4:], TELEGRAM_TOKEN)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid initData")
    return user["id"], user


@app.get("/api/health")
async def health():
    return {"status": "ok"}


class SessionResponse(BaseModel):
    user_id: int
    lang: str
    is_new: bool


@app.post("/api/auth/session", response_model=SessionResponse)
async def auth_session(authorization: str | None = Header(default=None)):
    uid, tg_user = _authed_user(authorization)
    is_new = db.ensure_user(
        uid,
        tg_user.get("username"),
        tg_user.get("first_name"),
        tg_user.get("language_code"),
    )
    row = db.get_user(uid)
    logger.info(f"session uid={uid} new={is_new}")
    return SessionResponse(user_id=uid, lang=row["lang"], is_new=is_new)
