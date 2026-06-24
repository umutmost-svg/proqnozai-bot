"""
Lightweight Flask dashboard for proqnozai-bot.
Run: python dashboard.py (separate process / Railway service)
Access: http://localhost:5000  — protected by DASHBOARD_TOKEN env var.
"""
import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, Response, render_template_string, request

app = Flask(__name__)

DB = os.environ.get("BOT_DB_PATH", "bot.db")
TOKEN = os.environ.get("DASHBOARD_TOKEN", "")   # set this in Railway env vars


# ─── Auth ─────────────────────────────────────────────────────────────────────
def require_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if TOKEN and request.args.get("token") != TOKEN:
            return Response("Unauthorized", 401, {"WWW-Authenticate": "Bearer"})
        return f(*args, **kwargs)
    return wrapper


# ─── DB helpers ───────────────────────────────────────────────────────────────
def _con():
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _q(sql, params=()):
    with _con() as c:
        return c.execute(sql, params).fetchall()


def _one(sql, params=()):
    with _con() as c:
        row = c.execute(sql, params).fetchone()
    return row[0] if row else 0


# ─── Data collectors ──────────────────────────────────────────────────────────
def collect():
    now = datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    users_total   = _one("SELECT COUNT(*) FROM users WHERE is_registered=1")
    users_today   = _one("SELECT COUNT(*) FROM users WHERE date(joined_at)=? AND is_registered=1", (today,))
    users_week    = _one("SELECT COUNT(*) FROM users WHERE date(joined_at)>=? AND is_registered=1", (week_ago,))
    users_blocked = _one("SELECT COUNT(*) FROM users WHERE is_blocked=1")
    users_active_today = _one(
        "SELECT COUNT(DISTINCT user_id) FROM requests WHERE date(created_at)=?", (today,))
    users_active_week  = _one(
        "SELECT COUNT(DISTINCT user_id) FROM requests WHERE date(created_at)>=?", (week_ago,))

    reqs_total = _one("SELECT COUNT(*) FROM requests")
    reqs_today = _one("SELECT COUNT(*) FROM requests WHERE date(created_at)=?", (today,))
    reqs_week  = _one("SELECT COUNT(*) FROM requests WHERE date(created_at)>=?", (week_ago,))

    forecasts_total = _one("SELECT COUNT(*) FROM forecast_history")
    forecasts_today = _one("SELECT COUNT(*) FROM forecast_history WHERE date(created_at)=?", (today,))

    fb_total = _one("SELECT COUNT(*) FROM forecast_history WHERE feedback IS NOT NULL")
    fb_wins  = _one("SELECT COUNT(*) FROM forecast_history WHERE feedback=1")
    fb_pct   = round(fb_wins / fb_total * 100) if fb_total else 0

    live_subs = _one("SELECT COUNT(*) FROM live_subscriptions")
    live_matches = _one("SELECT COUNT(DISTINCT match_id) FROM live_subscriptions")

    langs = _q("SELECT lang, COUNT(*) as cnt FROM users WHERE is_registered=1 GROUP BY lang ORDER BY cnt DESC")

    top_users = _q(
        "SELECT user_id, display_name, username, total_requests, last_active "
        "FROM users WHERE is_registered=1 ORDER BY total_requests DESC LIMIT 10")

    # Requests per day (last 14 days)
    daily = _q(
        "SELECT date(created_at) as d, COUNT(*) as cnt FROM requests "
        "WHERE date(created_at)>=? GROUP BY d ORDER BY d", (
            (now - timedelta(days=14)).strftime("%Y-%m-%d"),))

    # Recent registrations
    recent_users = _q(
        "SELECT user_id, display_name, username, lang, joined_at "
        "FROM users WHERE is_registered=1 ORDER BY joined_at DESC LIMIT 10")

    # Recent forecasts with feedback
    recent_forecasts = _q(
        "SELECT fh.user_id, u.display_name, fh.match_name, fh.feedback, fh.created_at "
        "FROM forecast_history fh LEFT JOIN users u ON fh.user_id=u.user_id "
        "ORDER BY fh.created_at DESC LIMIT 10")

    return dict(
        users_total=users_total, users_today=users_today, users_week=users_week,
        users_blocked=users_blocked, users_active_today=users_active_today,
        users_active_week=users_active_week,
        reqs_total=reqs_total, reqs_today=reqs_today, reqs_week=reqs_week,
        forecasts_total=forecasts_total, forecasts_today=forecasts_today,
        fb_total=fb_total, fb_wins=fb_wins, fb_pct=fb_pct,
        live_subs=live_subs, live_matches=live_matches,
        langs=langs, top_users=top_users, daily=daily,
        recent_users=recent_users, recent_forecasts=recent_forecasts,
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
    )


# ─── Template ─────────────────────────────────────────────────────────────────
TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Proqnozai Bot Dashboard</title>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
    --accent: #6c63ff; --green: #22c55e; --red: #ef4444;
    --yellow: #f59e0b; --text: #e2e8f0; --muted: #94a3b8;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 700; color: var(--accent); }
  header span { color: var(--muted); font-size: 12px; }
  .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 24px 0 12px; }
  .grid { display: grid; gap: 12px; }
  .g2 { grid-template-columns: repeat(2, 1fr); }
  .g3 { grid-template-columns: repeat(3, 1fr); }
  .g4 { grid-template-columns: repeat(4, 1fr); }
  @media(max-width:700px){ .g4,.g3,.g2{ grid-template-columns:1fr; } }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .stat-label { color: var(--muted); font-size: 12px; margin-bottom: 6px; }
  .stat-value { font-size: 28px; font-weight: 700; }
  .stat-sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .green { color: var(--green); }
  .red { color: var(--red); }
  .yellow { color: var(--yellow); }
  .accent { color: var(--accent); }
  table { width: 100%; border-collapse: collapse; }
  th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 600; }
  .badge-win { background: #14532d; color: var(--green); }
  .badge-lose { background: #450a0a; color: var(--red); }
  .badge-none { background: var(--border); color: var(--muted); }
  .bar-wrap { background: var(--border); border-radius: 4px; height: 6px; margin-top: 4px; }
  .bar { background: var(--accent); border-radius: 4px; height: 6px; }
  .chart { display: flex; align-items: flex-end; gap: 4px; height: 80px; padding-top: 8px; }
  .bar-col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px; }
  .bar-col .b { background: var(--accent); border-radius: 3px 3px 0 0; width: 100%; min-height: 2px; }
  .bar-col .l { color: var(--muted); font-size: 9px; writing-mode: vertical-rl; transform: rotate(180deg); }
  .refresh { color: var(--muted); font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>⚽ Proqnozai Bot</h1>
  <span class="refresh">Auto-refresh every 60s &nbsp;·&nbsp; {{ generated_at }}</span>
</header>
<div class="container">

  <h2>Users</h2>
  <div class="grid g4">
    <div class="card">
      <div class="stat-label">Total registered</div>
      <div class="stat-value accent">{{ users_total }}</div>
      <div class="stat-sub">{{ users_blocked }} blocked</div>
    </div>
    <div class="card">
      <div class="stat-label">New today</div>
      <div class="stat-value green">+{{ users_today }}</div>
      <div class="stat-sub">+{{ users_week }} this week</div>
    </div>
    <div class="card">
      <div class="stat-label">Active today (DAU)</div>
      <div class="stat-value">{{ users_active_today }}</div>
      <div class="stat-sub">{{ users_active_week }} this week (WAU)</div>
    </div>
    <div class="card">
      <div class="stat-label">Languages</div>
      <div class="stat-value">{{ langs|length }}</div>
      <div class="stat-sub">
        {% for l in langs[:4] %}{{ l[0] }}: {{ l[1] }}{% if not loop.last %} &nbsp; {% endif %}{% endfor %}
      </div>
    </div>
  </div>

  <h2>Requests &amp; Forecasts</h2>
  <div class="grid g4">
    <div class="card">
      <div class="stat-label">Total requests</div>
      <div class="stat-value">{{ reqs_total }}</div>
      <div class="stat-sub">{{ reqs_today }} today · {{ reqs_week }} this week</div>
    </div>
    <div class="card">
      <div class="stat-label">Forecasts generated</div>
      <div class="stat-value accent">{{ forecasts_total }}</div>
      <div class="stat-sub">{{ forecasts_today }} today</div>
    </div>
    <div class="card">
      <div class="stat-label">Feedback collected</div>
      <div class="stat-value">{{ fb_total }}</div>
      <div class="stat-sub">{{ fb_wins }} wins · {{ fb_pct }}% accuracy</div>
      <div class="bar-wrap"><div class="bar" style="width:{{ fb_pct }}%"></div></div>
    </div>
    <div class="card">
      <div class="stat-label">Live subscriptions</div>
      <div class="stat-value yellow">{{ live_subs }}</div>
      <div class="stat-sub">{{ live_matches }} unique matches</div>
    </div>
  </div>

  {% if daily %}
  <h2>Requests — last 14 days</h2>
  <div class="card">
    {% set max_cnt = daily|map(attribute=1)|max %}
    <div class="chart">
      {% for row in daily %}
      <div class="bar-col">
        <div class="b" style="height:{{ (row[1] / max_cnt * 70)|int }}px" title="{{ row[0] }}: {{ row[1] }}"></div>
        <div class="l">{{ row[0][5:] }}</div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <h2>Top 10 users by requests</h2>
  <div class="card">
    <table>
      <tr><th>#</th><th>User</th><th>ID</th><th>Requests</th><th>Last active</th></tr>
      {% for u in top_users %}
      <tr>
        <td class="muted">{{ loop.index }}</td>
        <td>{{ u[1] or u[2] or '—' }}</td>
        <td class="muted">{{ u[0] }}</td>
        <td class="accent">{{ u[3] }}</td>
        <td class="muted">{{ (u[4] or '')[:16] }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <div class="grid g2" style="margin-top:0">
    <div>
      <h2>Recent registrations</h2>
      <div class="card">
        <table>
          <tr><th>User</th><th>Lang</th><th>Joined</th></tr>
          {% for u in recent_users %}
          <tr>
            <td>{{ u[1] or u[2] or u[0] }}</td>
            <td>{{ u[3] }}</td>
            <td class="muted">{{ (u[4] or '')[:16] }}</td>
          </tr>
          {% endfor %}
        </table>
      </div>
    </div>
    <div>
      <h2>Recent forecasts</h2>
      <div class="card">
        <table>
          <tr><th>User</th><th>Match</th><th>Result</th></tr>
          {% for f in recent_forecasts %}
          <tr>
            <td>{{ f[1] or f[0] }}</td>
            <td>{{ (f[2] or '?')[:25] }}</td>
            <td>
              {% if f[3] == 1 %}<span class="badge badge-win">Win</span>
              {% elif f[3] == 0 %}<span class="badge badge-lose">Lose</span>
              {% else %}<span class="badge badge-none">—</span>{% endif %}
            </td>
          </tr>
          {% endfor %}
        </table>
      </div>
    </div>
  </div>

  <h2>Language distribution</h2>
  <div class="card">
    {% set total_lang = langs|sum(attribute=1) %}
    <table>
      <tr><th>Language</th><th>Users</th><th>Share</th></tr>
      {% for l in langs %}
      {% set pct = (l[1] / total_lang * 100)|round(1) if total_lang else 0 %}
      <tr>
        <td>{{ l[0] }}</td>
        <td>{{ l[1] }}</td>
        <td style="width:200px">
          <div style="display:flex;align-items:center;gap:8px">
            <div class="bar-wrap" style="flex:1"><div class="bar" style="width:{{ pct }}%"></div></div>
            <span class="muted">{{ pct }}%</span>
          </div>
        </td>
      </tr>
      {% endfor %}
    </table>
  </div>

</div>
</body>
</html>"""


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
@require_token
def index():
    try:
        data = collect()
    except Exception as e:
        return f"<pre>DB error: {e}</pre>", 500
    return render_template_string(TEMPLATE, **data)


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
