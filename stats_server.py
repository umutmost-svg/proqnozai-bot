"""
Lightweight stats HTTP server that runs inside the bot process.
Exposes GET /stats?token=X → JSON with all dashboard metrics.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from db import con

STATS_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
STATS_PORT  = int(os.environ.get("STATS_PORT", "8888"))


def _collect():
    from datetime import datetime, timedelta
    now = datetime.utcnow()
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
        "recent_users":       [[r[0],r[1],r[2],r[3],r[4]] for r in q("SELECT user_id,display_name,username,lang,joined_at FROM users WHERE is_registered=1 ORDER BY joined_at DESC LIMIT 10")],
        "recent_forecasts":   [[r[0],r[1],r[2],r[3],r[4]] for r in q("SELECT fh.user_id,u.display_name,fh.match_name,fh.feedback,fh.created_at FROM forecast_history fh LEFT JOIN users u ON fh.user_id=u.user_id ORDER BY fh.created_at DESC LIMIT 10")],
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # silence access log

    def do_GET(self):
        parsed = urlparse(self.path)
        token = parse_qs(parsed.query).get("token", [""])[0]

        if parsed.path == "/health":
            self._send(200, b"ok")
            return

        if parsed.path != "/stats":
            self._send(404, b"not found")
            return

        if STATS_TOKEN and token != STATS_TOKEN:
            self._send(401, b"unauthorized")
            return

        try:
            data = _collect()
            body = json.dumps(data, ensure_ascii=False).encode()
            self._send(200, body, "application/json")
        except Exception as e:
            self._send(500, str(e).encode())

    def _send(self, code, body, ct="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


def run_stats_server():
    """Run the stats HTTP server (blocking — call in a thread)."""
    server = HTTPServer(("0.0.0.0", STATS_PORT), _Handler)
    server.serve_forever()
