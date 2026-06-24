"""
Lightweight Flask dashboard for proqnozai-bot.
Reads metrics from the bot's internal stats endpoint (stats_server.py).
Run: python dashboard.py (separate Railway service)
Access: https://your-domain.railway.app/?token=DASHBOARD_TOKEN
"""
import os
from functools import wraps

import httpx
from flask import Flask, Response, render_template_string, request

app = Flask(__name__)

# URL of the bot's internal stats endpoint (Railway private network)
STATS_URL   = os.environ.get("STATS_URL", "http://worker.railway.internal:8888/stats")
STATS_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")


# ─── Auth ─────────────────────────────────────────────────────────────────────
def require_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if STATS_TOKEN and request.args.get("token") != STATS_TOKEN:
            return Response("Unauthorized", 401)
        return f(*args, **kwargs)
    return wrapper


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
  .g4 { grid-template-columns: repeat(4, 1fr); }
  @media(max-width:700px){ .g4,.g2{ grid-template-columns:1fr; } }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .stat-label { color: var(--muted); font-size: 12px; margin-bottom: 6px; }
  .stat-value { font-size: 28px; font-weight: 700; }
  .stat-sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .green { color: var(--green); } .red { color: var(--red); }
  .yellow { color: var(--yellow); } .accent { color: var(--accent); }
  .muted { color: var(--muted); }
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
</style>
</head>
<body>
<header>
  <h1>⚽ Proqnozai Bot</h1>
  <span>Auto-refresh every 60s</span>
</header>
<div class="container">

  <h2>Users</h2>
  <div class="grid g4">
    <div class="card">
      <div class="stat-label">Total registered</div>
      <div class="stat-value accent">{{ d.users_total }}</div>
      <div class="stat-sub">{{ d.users_blocked }} blocked</div>
    </div>
    <div class="card">
      <div class="stat-label">New today</div>
      <div class="stat-value green">+{{ d.users_today }}</div>
      <div class="stat-sub">+{{ d.users_week }} this week</div>
    </div>
    <div class="card">
      <div class="stat-label">Active today (DAU)</div>
      <div class="stat-value">{{ d.users_active_today }}</div>
      <div class="stat-sub">{{ d.users_active_week }} this week (WAU)</div>
    </div>
    <div class="card">
      <div class="stat-label">Languages</div>
      <div class="stat-value">{{ d.langs|length }}</div>
      <div class="stat-sub">
        {% for l in d.langs[:4] %}{{ l[0] }}: {{ l[1] }}{% if not loop.last %} &nbsp; {% endif %}{% endfor %}
      </div>
    </div>
  </div>

  <h2>Requests &amp; Forecasts</h2>
  <div class="grid g4">
    <div class="card">
      <div class="stat-label">Total requests</div>
      <div class="stat-value">{{ d.reqs_total }}</div>
      <div class="stat-sub">{{ d.reqs_today }} today · {{ d.reqs_week }} this week</div>
    </div>
    <div class="card">
      <div class="stat-label">Forecasts generated</div>
      <div class="stat-value accent">{{ d.forecasts_total }}</div>
      <div class="stat-sub">{{ d.forecasts_today }} today</div>
    </div>
    <div class="card">
      <div class="stat-label">Feedback accuracy</div>
      <div class="stat-value">{{ d.fb_pct }}%</div>
      <div class="stat-sub">{{ d.fb_wins }} wins / {{ d.fb_total }} rated</div>
      <div class="bar-wrap"><div class="bar" style="width:{{ d.fb_pct }}%"></div></div>
    </div>
    <div class="card">
      <div class="stat-label">Live subscriptions</div>
      <div class="stat-value yellow">{{ d.live_subs }}</div>
      <div class="stat-sub">{{ d.live_matches }} unique matches</div>
    </div>
  </div>

  {% if d.daily %}
  <h2>Requests — last 14 days</h2>
  <div class="card">
    {% set max_cnt = namespace(v=1) %}
    {% for row in d.daily %}{% if row[1] > max_cnt.v %}{% set max_cnt.v = row[1] %}{% endif %}{% endfor %}
    <div class="chart">
      {% for row in d.daily %}
      <div class="bar-col">
        <div class="b" style="height:{{ (row[1] / max_cnt.v * 70)|int }}px" title="{{ row[0] }}: {{ row[1] }}"></div>
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
      {% for u in d.top_users %}
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

  <div class="grid g2">
    <div>
      <h2>Recent registrations</h2>
      <div class="card">
        <table>
          <tr><th>User</th><th>Lang</th><th>Joined</th></tr>
          {% for u in d.recent_users %}
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
          {% for f in d.recent_forecasts %}
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
    {% set total_lang = namespace(v=0) %}
    {% for l in d.langs %}{% set total_lang.v = total_lang.v + l[1] %}{% endfor %}
    <table>
      <tr><th>Language</th><th>Users</th><th>Share</th></tr>
      {% for l in d.langs %}
      {% set pct = ((l[1] / total_lang.v * 100) | round(1)) if total_lang.v else 0 %}
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
    token_param = f"?token={STATS_TOKEN}" if STATS_TOKEN else ""
    try:
        resp = httpx.get(STATS_URL + token_param, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return f"<pre>Stats fetch error: {e}\nURL: {STATS_URL}</pre>", 500

    # compute fb_pct
    fb_total = raw.get("fb_total", 0)
    fb_wins  = raw.get("fb_wins", 0)
    raw["fb_pct"] = round(fb_wins / fb_total * 100) if fb_total else 0

    class D:
        def __getattr__(self, k): return raw.get(k)
    d = D()
    d.__dict__.update(raw)

    return render_template_string(TEMPLATE, d=d)


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
