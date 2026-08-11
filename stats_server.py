"""
Lightweight stats HTTP server that runs inside the bot process.

Every protected endpoint authenticates with the X-Dashboard-Token header —
never a query parameter, which would put the secret in proxy access logs.

  GET  /health           -> "ok" (unauthenticated, for platform health checks)
  GET  /stats            -> JSON with all dashboard metrics
  GET  /broadcast/status -> progress of the current/last broadcast
  GET  /users/search?q=  -> user lookup
  POST /broadcast        -> send a message to a user segment
  POST /users/block      -> block/unblock a user
"""
import asyncio
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from db import (con, _all, like_escape, db_log_partner_click,
                db_activation_funnel, db_engagement, db_retention,
                db_feedback_coverage, db_forecast_health, db_churn,
                db_promo_funnel, db_partner_clicks)

STATS_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
STATS_PORT  = int(os.environ.get("STATS_PORT", "8888"))

_bot_app  = None
_bot_loop = None

# Progress of the current/last broadcast, read via GET /broadcast/status.
_broadcast_state = {"running": False, "ok": 0, "fail": 0, "total": 0, "done": False}


def set_bot_app(app, loop):
    global _bot_app, _bot_loop
    _bot_app  = app
    _bot_loop = loop


def _auth_ok(token: str) -> bool:
    """Stats and admin endpoints must never run without an explicit token."""
    return bool(STATS_TOKEN) and hmac.compare_digest(token, STATS_TOKEN)


def _token_from(handler) -> str:
    """The token travels in the X-Dashboard-Token header and nowhere else.

    It used to be accepted from a ?token= query parameter, which put the secret
    into proxy access logs and browser history on every dashboard poll. The
    query fallback existed only to decouple worker and dashboard deploys; both
    now send the header, so a URL-borne token is simply unauthenticated."""
    return handler.headers.get("X-Dashboard-Token", "")


def _uids_for_seg(seg: str) -> list[int]:
    base = "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0"
    if seg == "all":
        rows = _all(base)
    elif seg.startswith("lang:"):
        rows = _all(base + " AND lang=?", (seg[5:],))
    elif seg == "act:active":
        rows = _all(base + " AND last_active != '' AND date(last_active) >= date('now', '-7 days')")
    elif seg == "act:churn":
        rows = _all(base + " AND last_active != '' AND date(last_active) < date('now', '-7 days')"
                         " AND date(last_active) >= date('now', '-30 days')")
    elif seg == "act:sleep":
        rows = _all(base + " AND (last_active='' OR date(last_active) < date('now', '-30 days'))")
    else:
        rows = []
    return [r[0] for r in rows]


def _user_search(qstr: str) -> list:
    rows = _all(
        "SELECT user_id, username, display_name, lang, is_blocked, total_requests, joined_at "
        "FROM users WHERE username LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\' "
        "OR CAST(user_id AS TEXT)=? ORDER BY total_requests DESC LIMIT 20",
        (f"%{like_escape(qstr)}%", f"%{like_escape(qstr)}%", qstr))
    return [dict(user_id=r[0], username=r[1], display_name=r[2], lang=r[3],
                 is_blocked=r[4], total_requests=r[5], joined_at=r[6]) for r in rows]


def _set_blocked(uid: int, blocked: int):
    with con() as c:
        c.execute("UPDATE users SET is_blocked=? WHERE user_id=?", (blocked, uid))


def _collect():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    def q(sql, p=()):
        with con() as c:
            return c.execute(sql, p).fetchall()

    def one(sql, p=()):
        with con() as c:
            r = c.execute(sql, p).fetchone()
        return r[0] if r else 0

    return {
        "users_total":        one("SELECT COUNT(*) FROM users WHERE is_registered=1"),
        "users_today":        one("SELECT COUNT(*) FROM users WHERE date(joined_at)=? AND is_registered=1", (today,)),
        "users_week":         one("SELECT COUNT(*) FROM users WHERE date(joined_at)>=? AND is_registered=1", (week_ago,)),
        "users_blocked":      one("SELECT COUNT(*) FROM users WHERE is_blocked=1"),
        "users_active_today": one("SELECT COUNT(DISTINCT user_id) FROM requests WHERE date(created_at)=?", (today,)),
        "users_active_week":  one("SELECT COUNT(DISTINCT user_id) FROM requests WHERE date(created_at)>=?", (week_ago,)),
        "reqs_total":         one("SELECT COUNT(*) FROM requests"),
        "reqs_today":         one("SELECT COUNT(*) FROM requests WHERE date(created_at)=?", (today,)),
        "reqs_week":          one("SELECT COUNT(*) FROM requests WHERE date(created_at)>=?", (week_ago,)),
        "forecasts_total":    one("SELECT COUNT(*) FROM forecast_history"),
        "forecasts_today":    one("SELECT COUNT(*) FROM forecast_history WHERE date(created_at)=?", (today,)),
        "fb_total":           one("SELECT COUNT(*) FROM forecast_history WHERE feedback IS NOT NULL"),
        "fb_wins":            one("SELECT COUNT(*) FROM forecast_history WHERE feedback=1"),
        "live_subs":          one("SELECT COUNT(*) FROM live_subscriptions"),
        "live_matches":       one("SELECT COUNT(DISTINCT match_id) FROM live_subscriptions"),
        "langs":              [[r[0], r[1]] for r in q("SELECT lang,COUNT(*) FROM users WHERE is_registered=1 GROUP BY lang ORDER BY 2 DESC")],
        "top_users":          [[r[0],r[1],r[2],r[3],r[4]] for r in q("SELECT user_id,display_name,username,total_requests,last_active FROM users WHERE is_registered=1 ORDER BY total_requests DESC LIMIT 10")],
        "daily":              [[r[0], r[1]] for r in q("SELECT date(created_at),COUNT(*) FROM requests WHERE date(created_at)>=? GROUP BY 1 ORDER BY 1", ((now - timedelta(days=14)).strftime("%Y-%m-%d"),))],
        "forecasts_daily":    [[r[0], r[1]] for r in q("SELECT date(created_at),COUNT(*) FROM forecast_history WHERE date(created_at)>=? GROUP BY 1 ORDER BY 1", ((now - timedelta(days=14)).strftime("%Y-%m-%d"),))],
        "winrate_daily":      [[r[0], r[1], r[2]] for r in q("SELECT date(created_at), SUM(CASE WHEN feedback=1 THEN 1 ELSE 0 END), COUNT(*) FROM forecast_history WHERE feedback IS NOT NULL AND date(created_at)>=? GROUP BY 1 ORDER BY 1", ((now - timedelta(days=14)).strftime("%Y-%m-%d"),))],
        "recent_users":       [[r[0],r[1],r[2],r[3],r[4]] for r in q("SELECT user_id,display_name,username,lang,joined_at FROM users WHERE is_registered=1 ORDER BY joined_at DESC LIMIT 10")],
        "recent_forecasts":   [[r[0],r[1],r[2],r[3],r[4]] for r in q("SELECT fh.user_id,u.display_name,fh.match_name,fh.feedback,fh.created_at FROM forecast_history fh LEFT JOIN users u ON fh.user_id=u.user_id ORDER BY fh.created_at DESC LIMIT 10")],
        # ── Product metrics ────────────────────────────────────────────
        # forecasts_* above come from forecast_history, which is capped at
        # 10 rows per user and therefore cannot measure volume. The block
        # below is computed from the uncapped `requests` event log.
        "funnel":             db_activation_funnel(),
        "engagement":         db_engagement(),
        "retention":          db_retention(),
        "feedback_coverage":  db_feedback_coverage(),
        "forecast_health":    db_forecast_health(),
        "churn":              db_churn(),
        "promo":              db_promo_funnel(),
        "partners":           db_partner_clicks(),
        "forecasts_real_total": one("SELECT COUNT(*) FROM requests WHERE msg_type='FORECAST'"),
        "forecasts_real_today": one("SELECT COUNT(*) FROM requests WHERE msg_type='FORECAST' AND date(created_at)=?", (today,)),
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # silence access log

    def do_GET(self):
        parsed = urlparse(self.path)
        token = _token_from(self)

        if parsed.path == "/health":
            self._send(200, b"ok")
            return

        if parsed.path == "/broadcast/status":
            if not _auth_ok(token):
                self._send(503 if not STATS_TOKEN else 401, b"dashboard token required"); return
            self._send(200, json.dumps(_broadcast_state).encode(), "application/json")
            return

        if parsed.path == "/users/search":
            if not _auth_ok(token):
                self._send(503 if not STATS_TOKEN else 401, b"dashboard token required"); return
            qstr = parse_qs(parsed.query).get("q", [""])[0].strip()
            rows = _user_search(qstr) if qstr else []
            self._send(200, json.dumps({"users": rows}, ensure_ascii=False).encode(),
                       "application/json")
            return

        if parsed.path != "/stats":
            self._send(404, b"not found")
            return

        if not _auth_ok(token):
            self._send(503 if not STATS_TOKEN else 401, b"dashboard token required")
            return

        try:
            data = _collect()
            body = json.dumps(data, ensure_ascii=False).encode()
            self._send(200, body, "application/json")
        except Exception as e:
            self._send(500, str(e).encode())

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/broadcast", "/users/block", "/track/partner_click"):
            self._send(404, b"not found"); return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send(400, b"invalid json"); return

        if not _auth_ok(_token_from(self)):
            self._send(503 if not STATS_TOKEN else 401, b"dashboard token required"); return

        if parsed.path == "/track/partner_click":
            # The dashboard forwards a partner click here; it has no DB access
            # of its own (web talks to the worker over HTTP only).
            try:
                uid = int(body.get("user_id") or 0)
            except (TypeError, ValueError):
                uid = 0
            db_log_partner_click(uid, str(body.get("partner") or ""))
            self._send(200, b"ok")
            return

        if parsed.path == "/users/block":
            try:
                uid = int(body.get("user_id"))
            except (TypeError, ValueError):
                self._send(400, b"bad user_id"); return
            blocked = 1 if body.get("blocked") else 0
            _set_blocked(uid, blocked)
            self._send(200, json.dumps({"user_id": uid, "blocked": blocked}).encode(),
                       "application/json")
            return

        text    = (body.get("text") or "").strip()
        segment = body.get("segment", "all")

        if not text:
            self._send(400, b"empty text"); return

        if not _bot_app or not _bot_loop:
            self._send(503, b"bot not ready"); return

        if _broadcast_state["running"]:
            self._send(409, json.dumps(_broadcast_state).encode(), "application/json"); return

        uids = _uids_for_seg(segment)

        async def _send_all():
            _broadcast_state.update(running=True, ok=0, fail=0, total=len(uids), done=False)
            for uid in uids:
                try:
                    await _bot_app.bot.send_message(chat_id=uid, text=text)
                    _broadcast_state["ok"] += 1
                except Exception:
                    _broadcast_state["fail"] += 1
                await asyncio.sleep(0.05)
            _broadcast_state.update(running=False, done=True)

        # Fire-and-forget: schedule on the bot loop and return immediately so the
        # dashboard never waits/times out. Progress is read via GET /broadcast/status.
        asyncio.run_coroutine_threadsafe(_send_all(), _bot_loop)
        result = json.dumps({"started": len(uids)})
        self._send(200, result.encode(), "application/json")

    def _send(self, code, body, ct="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


def run_stats_server():
    """Run the stats HTTP server (blocking — call in a thread).
    Threading: a slow /stats must not block Railway's /health checks."""
    server = ThreadingHTTPServer(("0.0.0.0", STATS_PORT), _Handler)
    server.serve_forever()
