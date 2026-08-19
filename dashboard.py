"""
Proqnozai Bot Dashboard — improved version.
Auth: HTTP Basic Auth (login: admin, password: DASHBOARD_TOKEN)
Stats source: bot's internal stats server (stats_server.py via Railway private network)
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from functools import wraps

from urllib.parse import urlparse

from partner_links import verify_click

import httpx
from flask import Flask, Response, render_template_string, request, redirect, url_for

app = Flask(__name__)
logger = logging.getLogger("dashboard")


def _safe_err(e: Exception) -> str:
    """A log-safe description of a backend error. httpx exception strings embed
    the request URL and can carry request detail with them, so we log only the
    exception type or the HTTP status — never the message (CLAUDE.md: never log
    token/key values). The token itself now travels in a header rather than the
    URL, but the rule stands: nothing from the exception body reaches the log."""
    resp = getattr(e, "response", None)
    status = getattr(resp, "status_code", None)
    return f"HTTP {status}" if status is not None else type(e).__name__


def _backend_error_json() -> Response:
    """503 JSON for an unreachable/failed stats backend. The exception detail is
    logged server-side but NEVER returned — the response must not leak the
    internal worker URL, the token, or a stack trace."""
    return Response(
        json.dumps({"error": "stats backend unavailable"}),
        status=503, mimetype="application/json")


_BACKEND_DOWN_PAGE = (
    "<div style='font-family:sans-serif;padding:40px;max-width:640px;margin:auto'>"
    "<h2>Dashboard temporarily unavailable</h2>"
    "<p>The stats service is not reachable right now. This usually clears on its "
    "own; if it persists, check that the bot worker is running.</p></div>"
)

_BOT_BASE     = os.environ.get("BOT_API_URL", "http://worker.railway.internal:8888")
STATS_URL     = os.environ.get("STATS_URL", _BOT_BASE + "/stats")
BROADCAST_URL = _BOT_BASE + "/broadcast"
STATS_TOKEN   = os.environ.get("DASHBOARD_TOKEN", "")
DASH_USER     = os.environ.get("DASHBOARD_USER", "admin")


# ─── Partner click redirect ───────────────────────────────────────────────────
# Only reachable when the bot is configured with PARTNER_REDIRECT_BASE pointing
# here; otherwise partner buttons link straight to the partner and this route is
# simply unused. Deliberately unauthenticated — the visitor is a bot user
# following a link, not an operator.
_PARTNER_TARGETS: dict = {}
_PARTNER_TARGETS_AT = 0.0
# Short enough that an edit made in another dashboard replica is picked up on
# its own; irrelevant in the normal case, where saving invalidates immediately.
_PARTNER_TARGETS_TTL = 30.0


def _invalidate_partner_targets() -> None:
    """Drop the redirect map so the next click re-reads it. Called after every
    successful write, which is what makes a saved URL take effect at once."""
    global _PARTNER_TARGETS, _PARTNER_TARGETS_AT
    _PARTNER_TARGETS = {}
    _PARTNER_TARGETS_AT = 0.0


def _partner_targets() -> dict:
    """name → destination URL, read from the worker (the DB is the source).

    The dashboard has no database of its own, so it asks the worker. Archived
    partners are included on purpose: their buttons live on in old Telegram
    messages forever, and a dead link is worse than a link to a partner we no
    longer advertise. On a worker error the previous map is kept — reaching the
    partner must not depend on our own bookkeeping being up."""
    global _PARTNER_TARGETS, _PARTNER_TARGETS_AT
    now = time.monotonic()
    if _PARTNER_TARGETS and now - _PARTNER_TARGETS_AT < _PARTNER_TARGETS_TTL:
        return _PARTNER_TARGETS
    try:
        resp = httpx.get(f"{_BOT_BASE}/partners", headers=_auth_headers(), timeout=5)
        resp.raise_for_status()
        payload = resp.json()
        # `targets` includes former names, so a link sent before a rename still
        # resolves. Fall back to the partner list if an older worker is still
        # answering (the two services deploy independently).
        rows = payload.get("targets")
        if not isinstance(rows, dict):
            rows = {p.get("name"): p.get("url") for p in payload.get("partners", [])}
        out = {}
        for name, url in rows.items():
            if name and isinstance(url, str) and url.startswith(("http://", "https://")):
                out[name] = url
        _PARTNER_TARGETS = out
        _PARTNER_TARGETS_AT = now
    except Exception as e:
        logger.warning("partner targets not refreshed: %s", _safe_err(e))
    return _PARTNER_TARGETS


@app.route("/r/<path:partner>")
def partner_redirect(partner):
    """Count the click, then send the user on. Recording is best-effort: a
    failing worker must never stop someone reaching the partner."""
    target = _partner_targets().get(partner)
    if not target:
        return Response("unknown partner", 404)

    uid = request.args.get("u")
    if verify_click(STATS_TOKEN, partner, uid, request.args.get("s", "")):
        try:
            httpx.post(f"{_BOT_BASE}/track/partner_click", headers=_auth_headers(),
                       json={"user_id": uid, "partner": partner}, timeout=2)
        except Exception as e:
            logger.warning("partner click not recorded: %s", _safe_err(e))
    else:
        # Unsigned or tampered — count nothing rather than let anyone inflate
        # the numbers by hitting this URL. The redirect itself still happens:
        # reaching the partner must never depend on our bookkeeping.
        logger.warning("partner click not counted: bad or missing signature")
    return redirect(target, code=302)


# ─── CSRF protection for state-changing requests ──────────────────────────────
# Basic Auth alone does not stop CSRF: the browser replays the credentials on a
# cross-site POST, so any page the logged-in admin visits could fire a broadcast
# to the whole user base. Two independent checks, either of which is enough:
#
#   * a token in the form, derived from DASHBOARD_TOKEN. An attacker's page
#     cannot read it (same-origin policy), so it cannot forge the POST.
#   * Origin / Referer must match the host actually serving the request.
#
# The token is stateless — there is no session to hang a nonce on — so it does
# not expire. That is acceptable here: knowing it requires already being able to
# read an authenticated page, which is the thing CSRF cannot do.
def csrf_token() -> str:
    return hmac.new(STATS_TOKEN.encode(), b"dashboard-csrf", hashlib.sha256).hexdigest()


def _same_origin() -> bool:
    """True when the request demonstrably came from our own page. A missing
    Origin AND Referer is treated as untrusted for state-changing methods."""
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not origin:
        return False
    return urlparse(origin).netloc == urlparse(request.host_url).netloc


def csrf_ok() -> bool:
    supplied = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
    return bool(STATS_TOKEN) and (
        hmac.compare_digest(supplied, csrf_token()) or _same_origin())


# ─── Basic Auth ───────────────────────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not STATS_TOKEN:
            return Response("DASHBOARD_TOKEN is required", 503)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, pwd = decoded.split(":", 1)
                # & (not `and`): constant-time, no short-circuit on wrong user.
                if hmac.compare_digest(user, DASH_USER) & hmac.compare_digest(pwd, STATS_TOKEN):
                    return f(*args, **kwargs)
            except Exception:
                pass
        return Response(
            "Требуется авторизация", 401,
            {"WWW-Authenticate": 'Basic realm="Proqnozai Dashboard"'}
        )
    return wrapper


# ─── Template ─────────────────────────────────────────────────────────────────
TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proqnozai — Дашборд</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
/* ── Themes ── */
:root {
  --bg:#0f1117; --bg2:#1a1d27; --border:#2a2d3a;
  --accent:#6c63ff; --accent2:#a78bfa;
  --green:#22c55e; --red:#ef4444; --yellow:#f59e0b; --blue:#38bdf8;
  --text:#e2e8f0; --muted:#94a3b8;
  --card-shadow: 0 2px 12px rgba(0,0,0,.35);
}
[data-theme="light"]{
  --bg:#f1f5f9; --bg2:#ffffff; --border:#e2e8f0;
  --accent:#6c63ff; --accent2:#7c3aed;
  --text:#0f172a; --muted:#64748b;
  --card-shadow: 0 2px 12px rgba(0,0,0,.08);
}
[data-theme="ocean"]{
  --bg:#0c1929; --bg2:#112236; --border:#1e3a5f;
  --accent:#38bdf8; --accent2:#0ea5e9;
  --text:#e0f2fe; --muted:#7dd3fc;
  --card-shadow: 0 2px 12px rgba(0,0,0,.4);
}
[data-theme="forest"]{
  --bg:#0a1612; --bg2:#122218; --border:#1e3a28;
  --accent:#22c55e; --accent2:#16a34a;
  --text:#dcfce7; --muted:#86efac;
  --card-shadow: 0 2px 12px rgba(0,0,0,.4);
}

*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;transition:background .3s,color .3s;}

/* ── Header ── */
header{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;backdrop-filter:blur(8px);}
.logo{display:flex;align-items:center;gap:10px;}
.logo-icon{font-size:22px;}
.logo h1{font-size:17px;font-weight:700;color:var(--accent);}
.logo small{color:var(--muted);font-size:11px;margin-left:8px;}
.header-right{display:flex;align-items:center;gap:12px;}
.refresh-badge{background:var(--border);color:var(--muted);font-size:11px;padding:4px 10px;border-radius:99px;}
.theme-btn{background:none;border:1px solid var(--border);color:var(--text);padding:5px 10px;border-radius:6px;cursor:pointer;font-size:12px;transition:all .2s;}
.theme-btn:hover{border-color:var(--accent);color:var(--accent);}
.theme-btn.active{background:var(--accent);color:#fff;border-color:var(--accent);}

/* ── Layout ── */
.container{max-width:1280px;margin:0 auto;padding:24px 20px;}
.section-title{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:28px 0 12px;font-weight:700;display:flex;align-items:center;gap:8px;}
.section-title::after{content:'';flex:1;height:1px;background:var(--border);}

/* ── Grid ── */
.grid{display:grid;gap:14px;}
.g2{grid-template-columns:repeat(2,1fr);}
.g3{grid-template-columns:repeat(3,1fr);}
.g4{grid-template-columns:repeat(4,1fr);}
.g5{grid-template-columns:repeat(5,1fr);}
@media(max-width:900px){.g5,.g4{grid-template-columns:repeat(2,1fr);}}
@media(max-width:600px){.g5,.g4,.g3,.g2{grid-template-columns:1fr;}}

/* ── Cards ── */
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:18px;box-shadow:var(--card-shadow);transition:border-color .2s;}
.chart-fallback{color:var(--muted);font-size:13px;padding:24px 8px;text-align:center;}
.card:hover{border-color:var(--accent);}

/* ── Stat cards ── */
.stat-card{position:relative;overflow:hidden;}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--accent),var(--accent2));}
.stat-label{color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px;}
.stat-value{font-size:32px;font-weight:800;line-height:1;margin-bottom:4px;}
.stat-sub{color:var(--muted);font-size:12px;}
.stat-icon{position:absolute;right:16px;top:16px;font-size:28px;opacity:.15;}
.green{color:var(--green);} .red{color:var(--red);}
.yellow{color:var(--yellow);} .accent{color:var(--accent);} .blue{color:var(--blue);}
.muted{color:var(--muted);}

/* ── Progress bar ── */
.bar-wrap{background:var(--border);border-radius:4px;height:6px;margin-top:8px;overflow:hidden;}
.bar{border-radius:4px;height:6px;transition:width .6s ease;}
.bar-accent{background:linear-gradient(90deg,var(--accent),var(--accent2));}
.bar-green{background:var(--green);}

/* ── Table ── */
table{width:100%;border-collapse:collapse;}
th{color:var(--muted);font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:.08em;padding:10px 12px;text-align:left;border-bottom:1px solid var(--border);}
td{padding:10px 12px;border-bottom:1px solid var(--border);font-size:13px;transition:background .15s;}
tr:hover td{background:rgba(108,99,255,.05);}
tr:last-child td{border-bottom:none;}

/* ── Badges ── */
.badge{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:99px;font-size:11px;font-weight:700;}
.badge-win{background:#14532d;color:var(--green);}
.badge-lose{background:#450a0a;color:var(--red);}
.badge-none{background:var(--border);color:var(--muted);}
.badge-lang{background:rgba(108,99,255,.15);color:var(--accent);font-size:10px;padding:2px 7px;}

/* ── KPI row ── */
.kpi-delta{font-size:12px;font-weight:600;padding:2px 7px;border-radius:6px;margin-left:6px;}
.kpi-up{background:#14532d;color:var(--green);}
.kpi-zero{background:var(--border);color:var(--muted);}

/* ── Chart containers ── */
.chart-wrap{position:relative;height:200px;}
.chart-wrap-sm{position:relative;height:160px;}

/* ── Rank number ── */
.rank{width:24px;height:24px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;}
.rank-1{background:linear-gradient(135deg,#f59e0b,#fbbf24);color:#000;}
.rank-2{background:linear-gradient(135deg,#94a3b8,#cbd5e1);color:#000;}
.rank-3{background:linear-gradient(135deg,#b45309,#d97706);color:#fff;}
.rank-n{background:var(--border);color:var(--muted);}

/* ── Footer ── */
footer{text-align:center;color:var(--muted);font-size:11px;padding:24px;border-top:1px solid var(--border);margin-top:32px;}

/* ── Pulse dot ── */
.pulse{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 rgba(34,197,94,.4);animation:pulse 2s infinite;}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,.4);}70%{box-shadow:0 0 0 8px rgba(34,197,94,0);}100%{box-shadow:0 0 0 0 rgba(34,197,94,0);}}
</style>
</head>
<body data-theme="dark">
<header>
  <div class="logo">
    <span class="logo-icon">⚽</span>
    <div>
      <h1>Proqnozai Bot</h1>
      <small><span class="pulse"></span> &nbsp;Онлайн · Обновление каждые 60с</small>
    </div>
  </div>
  <div class="header-right">
    <a href="/" class="theme-btn" style="text-decoration:none">📊 Статистика</a>
    <a href="/users" class="theme-btn" style="text-decoration:none">👥 Пользователи</a>
    <a href="/partners" class="theme-btn" style="text-decoration:none">🤝 Партнёры</a>
    <a href="/broadcast" class="theme-btn" style="text-decoration:none">📢 Рассылка</a>
    <span class="refresh-badge">{{ generated_at }}</span>
    <button class="theme-btn active" onclick="setTheme('dark')">🌙 Тёмная</button>
    <button class="theme-btn" onclick="setTheme('light')">☀️ Светлая</button>
    <button class="theme-btn" onclick="setTheme('ocean')">🌊 Океан</button>
    <button class="theme-btn" onclick="setTheme('forest')">🌿 Лес</button>
  </div>
</header>

<div class="container">

  <!-- ── KPI ── -->
  <div class="section-title">Ключевые показатели</div>
  <div class="grid g5">
    <div class="card stat-card">
      <span class="stat-icon">👥</span>
      <div class="stat-label">Всего пользователей</div>
      <div class="stat-value accent" id="k_users">{{ d.users_total }}</div>
      <div class="stat-sub">{{ d.users_blocked }} заблокировано</div>
    </div>
    <div class="card stat-card">
      <span class="stat-icon">✨</span>
      <div class="stat-label">Новых сегодня</div>
      <div class="stat-value green" id="k_new">+{{ d.users_today }}</div>
      <div class="stat-sub">+{{ d.users_week }} за неделю</div>
    </div>
    <div class="card stat-card">
      <span class="stat-icon">🔥</span>
      <div class="stat-label">Активны сегодня (DAU)</div>
      <div class="stat-value" id="k_dau">{{ d.users_active_today }}</div>
      <div class="stat-sub">{{ d.users_active_week }} за неделю (WAU)</div>
    </div>
    <div class="card stat-card">
      <span class="stat-icon">📊</span>
      <div class="stat-label">Прогнозов всего</div>
      <div class="stat-value accent" id="k_forecasts">{{ d.forecasts_real_total|default(d.forecasts_total) }}</div>
      <div class="stat-sub">{{ d.forecasts_today }} сегодня</div>
    </div>
    <div class="card stat-card">
      <span class="stat-icon">🎯</span>
      <div class="stat-label">Точность (feedback)</div>
      <div class="stat-value {% if d.fb_pct >= 60 %}green{% elif d.fb_pct >= 40 %}yellow{% else %}red{% endif %}">{{ d.fb_pct }}%</div>
      <div class="stat-sub">{{ d.fb_wins }} побед / {{ d.fb_total }} оценок</div>
      <div class="bar-wrap"><div class="bar bar-{% if d.fb_pct >= 60 %}green{% else %}accent{% endif %}" style="width:{{ d.fb_pct }}%"></div></div>
    </div>
  </div>

  <!-- ── Запросы и live ── -->
  <div class="grid g3" style="margin-top:14px;">
    <div class="card stat-card">
      <span class="stat-icon">📨</span>
      <div class="stat-label">Запросов всего</div>
      <div class="stat-value">{{ d.reqs_total }}</div>
      <div class="stat-sub">{{ d.reqs_today }} сегодня · {{ d.reqs_week }} за неделю</div>
    </div>
    <div class="card stat-card">
      <span class="stat-icon">📡</span>
      <div class="stat-label">Live-подписок</div>
      <div class="stat-value yellow">{{ d.live_subs }}</div>
      <div class="stat-sub">{{ d.live_matches }} уникальных матчей</div>
    </div>
    <div class="card stat-card">
      <span class="stat-icon">🌍</span>
      <div class="stat-label">Языков</div>
      <div class="stat-value">{{ d.langs|length }}</div>
      <div class="stat-sub">
        {% for l in d.langs[:4] %}<span class="badge badge-lang">{{ l[0] }} {{ l[1] }}</span> {% endfor %}
      </div>
    </div>
  </div>

  <!-- ── Продуктовые метрики ── -->
  {% set f  = d.funnel|default({}) %}
  {% set e  = d.engagement|default({}) %}
  {% set fh = d.forecast_health|default({}) %}
  {% set fc = d.feedback_coverage|default({}) %}
  {% set pr = d.promo|default({}) %}
  {% set pt = d.partners|default({}) %}
  {% set ch = d.churn|default({}) %}
  {% if e %}
  <div class="section-title">Продукт</div>
  <div class="grid g4">
    <div class="card stat-card">
      <div class="stat-label">DAU / WAU / MAU</div>
      <div class="stat-value">{{ e.dau }}<span class="muted" style="font-size:18px"> / {{ e.wau }} / {{ e.mau }}</span></div>
      <div class="stat-sub">Липучесть DAU/MAU: <b class="{{ 'green' if e.stickiness >= 20 else 'yellow' }}">{{ e.stickiness }}%</b></div>
    </div>
    <div class="card stat-card">
      <div class="stat-label">Прогнозов всего</div>
      <div class="stat-value">{{ d.forecasts_real_total|default(0) }}</div>
      <div class="stat-sub">сегодня {{ d.forecasts_real_today|default(0) }} · на активного за неделю {{ e.forecasts_per_wau }}</div>
    </div>
    <div class="card stat-card">
      <div class="stat-label">Успешность генерации</div>
      <div class="stat-value {{ 'green' if fh.ok_pct|default(0) >= 95 else 'red' }}">{{ fh.ok_pct|default(0) }}%</div>
      <div class="stat-sub">за 7 дней: {{ fh.ok|default(0) }} из {{ fh.total|default(0) }} · сбоев {{ fh.failed|default(0) }}</div>
    </div>
    <div class="card stat-card">
      <div class="stat-label">Время генерации</div>
      <div class="stat-value">{{ (fh.p50_ms|default(0) / 1000)|round(1) }}<span class="muted" style="font-size:18px"> с</span></div>
      <div class="stat-sub">медиана · среднее {{ (fh.avg_ms|default(0) / 1000)|round(1) }} с</div>
    </div>
  </div>

  <div class="grid g2" style="margin-top:14px">
    <div class="card">
      <div class="stat-label" style="margin-bottom:12px">Воронка активации</div>
      {% set base = f.started|default(1) or 1 %}
      {% for name, val in [('Пришли', f.started|default(0)), ('Зарегистрировались', f.registered|default(0)),
                           ('Прошли онбординг', f.onboarded|default(0)), ('Получили прогноз', f.forecasted|default(0))] %}
      <div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;font-size:13px">
          <span>{{ name }}</span><span><b>{{ val }}</b> <span class="muted">{{ (val / base * 100)|round|int }}%</span></span>
        </div>
        <div class="bar-wrap"><div class="bar bar-accent" style="width:{{ (val / base * 100)|round|int }}%"></div></div>
      </div>
      {% endfor %}
    </div>

    <div class="card">
      <div class="stat-label" style="margin-bottom:12px">Удержание по когортам</div>
      <table>
        <tr><th>Дата</th><th>Когорта</th><th>D1</th><th>D7</th><th>D30</th></tr>
        {% for r in (d.retention|default([]))[:7] %}
        <tr><td>{{ r.day }}</td><td>{{ r.size }}</td>
            <td>{% if r.d1 is none %}<span class="muted">—</span>{% else %}{{ r.d1 }}%{% endif %}</td>
            <td>{% if r.d7 is none %}<span class="muted">—</span>{% else %}{{ r.d7 }}%{% endif %}</td>
            <td>{% if r.d30 is none %}<span class="muted">—</span>{% else %}{{ r.d30 }}%{% endif %}</td></tr>
        {% else %}
        <tr><td colspan="5" class="muted">Пока нет данных</td></tr>
        {% endfor %}
      </table>
    </div>
  </div>

  <div class="grid g3" style="margin-top:14px">
    <div class="card">
      <div class="stat-label" style="margin-bottom:10px">Партнёры (30 дней)</div>
      <div class="stat-value" style="font-size:26px">{{ pt.total|default(0) }}</div>
      <div class="stat-sub" style="margin-bottom:10px">
        кликов · уникальных {{ pt.unique_users|default(0) }} · открывали список {{ pt.opened_list|default(0) }}
        {% if pt.opened_list|default(0) %}· конверсия {{ pt.click_through }}%{% endif %}
      </div>
      {% if pt.by_partner|default([]) %}
      <table>
        <tr><th>Партнёр</th><th>Кликов</th><th>Людей</th></tr>
        {% for name, clicks, users in pt.by_partner %}
        <tr><td>{{ name }}</td><td>{{ clicks }}</td><td>{{ users }}</td></tr>
        {% endfor %}
      </table>
      {% else %}
      <div class="muted" style="font-size:12px">Трекинг кликов выключен (PARTNER_REDIRECT_BASE не задан)</div>
      {% endif %}
    </div>

    <div class="card">
      <div class="stat-label" style="margin-bottom:10px">Промокоды</div>
      {% if pr.partners %}
      <div class="stat-value" style="font-size:26px">{{ pr.claimed }}<span class="muted" style="font-size:16px"> / {{ pr.max_uses }}</span></div>
      <div class="bar-wrap"><div class="bar bar-green" style="width:{{ (pr.claimed / (pr.max_uses or 1) * 100)|round|int }}%"></div></div>
      <div class="stat-sub" style="margin:8px 0 10px">
        получили {{ pr.users }} чел. · за неделю {{ pr.claimed_7d }} · конверсия от базы {{ pr.conversion }}%
      </div>
      <table>
        <tr><th>Партнёр</th><th>Код</th><th>Выдано</th></tr>
        {% for c in pr.partners %}
        <tr><td>{{ c.partner or '—' }}</td><td>{{ c.code }}</td>
            <td>{{ c.claimed }}/{{ c.max_uses }}</td></tr>
        {% endfor %}
      </table>
      {% else %}
      <div class="muted" style="font-size:12px">Активных кампаний нет</div>
      {% endif %}
    </div>

    <div class="card">
      <div class="stat-label" style="margin-bottom:10px">Отток и обратная связь</div>
      <div class="stat-sub" style="line-height:1.9">
        Активны за 7 дней: <b class="green">{{ ch.active_7d|default(0) }}</b><br>
        Молчат 7–30 дней: <b class="yellow">{{ ch.silent_7_30|default(0) }}</b><br>
        Молчат больше 30: <b class="red">{{ ch.silent_30|default(0) }}</b><br>
        Ни одного действия: <b class="muted">{{ ch.never|default(0) }}</b><br>
        Оценено прогнозов: <b>{{ fc.pct|default(0) }}%</b> <span class="muted">({{ fc.rated|default(0) }} из {{ fc.total|default(0) }})</span>
      </div>
    </div>
  </div>
  {% endif %}

  <!-- ── Графики ── -->
  <div class="section-title">Аналитика</div>
  <div class="grid g3">

    <div class="card" style="grid-column: span 2;">
      <div style="font-weight:600;margin-bottom:12px;">📈 Запросы за 14 дней</div>
      <div class="chart-wrap">
        <canvas id="lineChart"></canvas>
      </div>
    </div>

    <div class="card">
      <div style="font-weight:600;margin-bottom:12px;">🌍 Языки пользователей</div>
      <div class="chart-wrap">
        <canvas id="langChart"></canvas>
      </div>
    </div>

    <div class="card" style="grid-column: span 2;">
      <div style="font-weight:600;margin-bottom:12px;">📊 Прогнозы по дням</div>
      <div class="chart-wrap-sm">
        <canvas id="barChart"></canvas>
      </div>
    </div>

    <div class="card">
      <div style="font-weight:600;margin-bottom:12px;">🎯 Результаты прогнозов</div>
      <div class="chart-wrap-sm">
        <canvas id="feedbackChart"></canvas>
      </div>
    </div>

    <div class="card" style="grid-column: span 3;">
      <div style="font-weight:600;margin-bottom:12px;">📈 Точность (win-rate) за 14 дней, %</div>
      <div class="chart-wrap-sm">
        <canvas id="winrateChart"></canvas>
      </div>
    </div>

  </div>

  <!-- ── Топ пользователей ── -->
  <div class="section-title">Топ пользователей</div>
  <div class="card">
    <table>
      <tr><th></th><th>Пользователь</th><th>ID</th><th>Запросов</th><th>Последняя активность</th></tr>
      {% for u in d.top_users %}
      <tr>
        <td>
          {% if loop.index == 1 %}<span class="rank rank-1">1</span>
          {% elif loop.index == 2 %}<span class="rank rank-2">2</span>
          {% elif loop.index == 3 %}<span class="rank rank-3">3</span>
          {% else %}<span class="rank rank-n">{{ loop.index }}</span>{% endif %}
        </td>
        <td><strong>{{ u[1] or u[2] or '—' }}</strong></td>
        <td class="muted">{{ u[0] }}</td>
        <td><span class="accent" style="font-weight:700;">{{ u[3] }}</span></td>
        <td class="muted">{{ (u[4] or '')[:16] }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <!-- ── Последние события ── -->
  <div class="grid g2">
    <div>
      <div class="section-title">Новые пользователи</div>
      <div class="card">
        <table>
          <tr><th>Пользователь</th><th>Язык</th><th>Дата</th></tr>
          {% for u in d.recent_users %}
          <tr>
            <td><strong>{{ u[1] or u[2] or u[0] }}</strong></td>
            <td><span class="badge badge-lang">{{ u[3] }}</span></td>
            <td class="muted">{{ (u[4] or '')[:16] }}</td>
          </tr>
          {% endfor %}
        </table>
      </div>
    </div>
    <div>
      <div class="section-title">Последние прогнозы</div>
      <div class="card">
        <table>
          <tr><th>Пользователь</th><th>Матч</th><th>Результат</th></tr>
          {% for f in d.recent_forecasts %}
          <tr>
            <td><strong>{{ f[1] or f[0] }}</strong></td>
            <td class="muted">{{ (f[2] or '?')[:22] }}</td>
            <td>
              {% if f[3] == 1 %}<span class="badge badge-win">✓ Победа</span>
              {% elif f[3] == 0 %}<span class="badge badge-lose">✗ Проигрыш</span>
              {% else %}<span class="badge badge-none">— Нет</span>{% endif %}
            </td>
          </tr>
          {% endfor %}
        </table>
      </div>
    </div>
  </div>

  <!-- ── Языки детально ── -->
  <div class="section-title">Распределение по языкам</div>
  <div class="card">
    {% set total_lang = namespace(v=0) %}
    {% for l in d.langs %}{% set total_lang.v = total_lang.v + l[1] %}{% endfor %}
    <table>
      <tr><th>Язык</th><th>Пользователей</th><th>Доля</th><th></th></tr>
      {% for l in d.langs %}
      {% set pct = ((l[1] / total_lang.v * 100)|round(1)) if total_lang.v else 0 %}
      <tr>
        <td><span class="badge badge-lang">{{ l[0] }}</span></td>
        <td><strong>{{ l[1] }}</strong></td>
        <td class="muted">{{ pct }}%</td>
        <td style="width:220px">
          <div class="bar-wrap"><div class="bar bar-accent" style="width:{{ pct }}%"></div></div>
        </td>
      </tr>
      {% endfor %}
    </table>
  </div>

</div>

<footer>Proqnozai Bot Dashboard · Обновляется автоматически каждые 60 секунд</footer>

<script>
// ── Theme switcher ──────────────────────────────────────────────────────────
function setTheme(t) {
  document.body.setAttribute('data-theme', t);
  localStorage.setItem('theme', t);
  document.querySelectorAll('.theme-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  updateCharts();
}
(function(){
  const saved = localStorage.getItem('theme') || 'dark';
  document.body.setAttribute('data-theme', saved);
  document.querySelectorAll('.theme-btn').forEach(b => {
    if(b.textContent.includes(saved === 'dark' ? '🌙' : saved === 'light' ? '☀️' : saved === 'ocean' ? '🌊' : '🌿'))
      b.classList.add('active');
    else b.classList.remove('active');
  });
})();

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

// ── Chart data ──────────────────────────────────────────────────────────────
let dailyLabels = {{ daily_labels|tojson }};
let dailyData   = {{ daily_values|tojson }};
let langLabels  = {{ lang_labels|tojson }};
let langData    = {{ lang_values|tojson }};
let fbData      = {{ fb_data|tojson }};
let winrateLabels = {{ winrate_labels|tojson }};
let winrateData   = {{ winrate_values|tojson }};

const COLORS = ['#6c63ff','#38bdf8','#22c55e','#f59e0b','#ef4444','#a78bfa','#fb923c'];

let lineChart, langChart, barChart, feedbackChart, winrateChart;

// Chart.js is loaded from a CDN. If that host is unreachable the global is
// undefined, and the first Chart.* access used to throw — killing the rest of
// this script along with auto-refresh, the theme toggle and user search. The
// numbers, tables and controls do not need the library, so a missing Chart
// costs the graphs only.
function chartsAvailable() { return typeof Chart !== 'undefined'; }

function noteChartsUnavailable() {
  document.querySelectorAll('canvas').forEach(cv => {
    const note = document.createElement('div');
    note.className = 'chart-fallback';
    note.textContent = 'Графики недоступны: не загрузилась библиотека Chart.js.';
    if (cv.parentNode) cv.parentNode.replaceChild(note, cv);
  });
}

function makeCharts() {
  if (!chartsAvailable()) { noteChartsUnavailable(); return; }
  const gridColor = () => cssVar('--border');
  const textColor = () => cssVar('--muted');
  const accent    = () => cssVar('--accent');

  Chart.defaults.color = textColor();
  Chart.defaults.borderColor = gridColor();

  // Line chart — requests per day
  lineChart = new Chart(document.getElementById('lineChart'), {
    type: 'line',
    data: {
      labels: dailyLabels,
      datasets: [{
        label: 'Запросы',
        data: dailyData,
        borderColor: accent(),
        backgroundColor: accent() + '22',
        borderWidth: 2,
        pointRadius: 3,
        fill: true,
        tension: 0.4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: gridColor() }, ticks: { color: textColor(), maxTicksLimit: 7 } },
        y: { grid: { color: gridColor() }, ticks: { color: textColor() }, beginAtZero: true }
      }
    }
  });

  // Donut — languages
  langChart = new Chart(document.getElementById('langChart'), {
    type: 'doughnut',
    data: {
      labels: langLabels,
      datasets: [{ data: langData, backgroundColor: COLORS, borderWidth: 2, borderColor: cssVar('--bg2') }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom', labels: { color: textColor(), padding: 10, font: { size: 11 } } } },
      cutout: '65%'
    }
  });

  // Bar — same daily data as second view
  barChart = new Chart(document.getElementById('barChart'), {
    type: 'bar',
    data: {
      labels: dailyLabels,
      datasets: [{
        label: 'Запросы',
        data: dailyData,
        backgroundColor: accent() + 'cc',
        borderRadius: 6,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: textColor(), maxTicksLimit: 7 } },
        y: { grid: { color: gridColor() }, ticks: { color: textColor() }, beginAtZero: true }
      }
    }
  });

  // Donut — feedback
  feedbackChart = new Chart(document.getElementById('feedbackChart'), {
    type: 'doughnut',
    data: {
      labels: ['Победы', 'Проигрыши', 'Без оценки'],
      datasets: [{ data: fbData, backgroundColor: ['#22c55e','#ef4444','#374151'], borderWidth: 2, borderColor: cssVar('--bg2') }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom', labels: { color: textColor(), padding: 10, font: { size: 11 } } } },
      cutout: '65%'
    }
  });

  // Line — win-rate trend
  winrateChart = new Chart(document.getElementById('winrateChart'), {
    type: 'line',
    data: {
      labels: winrateLabels,
      datasets: [{
        label: 'Win-rate %', data: winrateData,
        borderColor: cssVar('--green'), backgroundColor: cssVar('--green') + '22',
        borderWidth: 2, pointRadius: 3, fill: true, tension: 0.4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: textColor(), maxTicksLimit: 7 } },
        y: { grid: { color: gridColor() }, ticks: { color: textColor() }, beginAtZero: true, max: 100 }
      }
    }
  });
}

function updateCharts() {
  if (!chartsAvailable()) return;   // nothing to redraw; the rest still refreshes
  [lineChart, langChart, barChart, feedbackChart, winrateChart].forEach(c => { if(c) c.destroy(); });
  setTimeout(makeCharts, 50);
}

makeCharts();

// ── AJAX auto-refresh (no page reload, keeps theme & scroll) ─────────────────
function setText(id, v){ const el = document.getElementById(id); if(el) el.textContent = v; }
async function refreshData(){
  try{
    const r = await fetch('/api/data'); if(!r.ok) return;
    const x = await r.json();
    setText('k_users', x.users_total);
    setText('k_new', '+' + (x.users_today||0));
    setText('k_dau', x.users_active_today);
    setText('k_forecasts', x.forecasts_real_total ?? x.forecasts_total);
    const fbt = x.fb_total||0, fbw = x.fb_wins||0;
    setText('k_acc', (fbt ? Math.round(fbw/fbt*100) : 0) + '%');
    dailyLabels = (x.daily||[]).map(r=>r[0].slice(5));
    dailyData   = (x.daily||[]).map(r=>r[1]);
    langLabels  = (x.langs||[]).map(r=>r[0]);
    langData    = (x.langs||[]).map(r=>r[1]);
    fbData      = [fbw, fbt-fbw, Math.max(0,(x.forecasts_total||0)-fbt)];
    winrateLabels = (x.winrate_daily||[]).map(r=>r[0].slice(5));
    winrateData   = (x.winrate_daily||[]).map(r=> r[2] ? Math.round(r[1]/r[2]*100) : 0);
    updateCharts();
    const badge = document.querySelector('.refresh-badge');
    if(badge) badge.textContent = '🔄 ' + new Date().toLocaleTimeString('ru-RU');
  }catch(e){}
}
setInterval(refreshData, 45000);
</script>
</body>
</html>"""


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
@require_auth
def index():
    try:
        resp = httpx.get(STATS_URL, headers=_auth_headers(), timeout=10)
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, dict):
            # A reachable-but-malformed 200 (valid JSON that isn't an object)
            # must degrade to the safe page, not crash the render below.
            raise ValueError("unexpected stats payload shape")
    except Exception as e:
        # Log the detail (server-side only); never leak the internal URL/token or
        # a stack trace to the browser. Degrade to a safe placeholder page.
        logger.warning("stats backend unavailable for '/': %s", _safe_err(e))
        return _BACKEND_DOWN_PAGE, 503

    fb_total = raw.get("fb_total", 0)
    fb_wins  = raw.get("fb_wins", 0)
    fb_lose  = fb_total - fb_wins
    raw["fb_pct"] = round(fb_wins / fb_total * 100) if fb_total else 0

    daily       = raw.get("daily", [])
    daily_labels = [r[0][5:] for r in daily]
    daily_values = [r[1] for r in daily]

    langs       = raw.get("langs", [])
    lang_labels = [r[0] for r in langs]
    lang_values = [r[1] for r in langs]

    forecasts_total = raw.get("forecasts_total", 0)
    fb_unrated = max(0, forecasts_total - fb_total)

    wr = raw.get("winrate_daily", [])
    winrate_labels = [r[0][5:] for r in wr]
    winrate_values = [round(r[1] / r[2] * 100) if r[2] else 0 for r in wr]

    from datetime import datetime, timezone
    generated_at = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    class D:
        pass
    d = D()
    d.__dict__.update(raw)

    return render_template_string(
        TEMPLATE, d=d,
        daily_labels=daily_labels, daily_values=daily_values,
        lang_labels=lang_labels, lang_values=lang_values,
        fb_data=[fb_wins, fb_lose, fb_unrated],
        winrate_labels=winrate_labels, winrate_values=winrate_values,
        generated_at=generated_at,
    )


def _auth_headers() -> dict:
    """Token goes in a header, never in the URL: a query string lands in proxy
    access logs and browser history. The worker still accepts ?token= so the two
    services can be redeployed in either order."""
    return {"X-Dashboard-Token": STATS_TOKEN} if STATS_TOKEN else {}


@app.route("/api/data")
@require_auth
def api_data():
    """JSON stats for the dashboard's AJAX auto-refresh."""
    try:
        resp = httpx.get(STATS_URL, headers=_auth_headers(), timeout=10)
        resp.raise_for_status()
        return Response(resp.text, mimetype="application/json")
    except Exception as e:
        logger.warning("stats backend unavailable for API route: %s", _safe_err(e))
        return _backend_error_json()


@app.route("/api/broadcast/status")
@require_auth
def api_broadcast_status():
    try:
        resp = httpx.get(f"{_BOT_BASE}/broadcast/status", headers=_auth_headers(), timeout=8)
        return Response(resp.text, mimetype="application/json", status=resp.status_code)
    except Exception as e:
        logger.warning("stats backend unavailable for API route: %s", _safe_err(e))
        return _backend_error_json()


@app.route("/api/users/search")
@require_auth
def api_users_search():
    q = request.args.get("q", "").strip()
    try:
        resp = httpx.get(f"{_BOT_BASE}/users/search", params={"q": q},
                         headers=_auth_headers(), timeout=8)
        return Response(resp.text, mimetype="application/json", status=resp.status_code)
    except Exception as e:
        logger.warning("stats backend unavailable for API route: %s", _safe_err(e))
        return _backend_error_json()


@app.route("/api/users/block", methods=["POST"])
@require_auth
def api_users_block():
    if not csrf_ok():
        logger.warning("user block rejected: CSRF check failed")
        return Response("CSRF check failed", 403)
    body = request.get_json(silent=True) or {}
    try:
        resp = httpx.post(f"{_BOT_BASE}/users/block", headers=_auth_headers(), json={
            "user_id": body.get("user_id"),
            "blocked": body.get("blocked"),
        }, timeout=8)
        return Response(resp.text, mimetype="application/json", status=resp.status_code)
    except Exception as e:
        logger.warning("stats backend unavailable for API route: %s", _safe_err(e))
        return _backend_error_json()


# ─── Partners & promo ─────────────────────────────────────────────────────────
# Thin proxy, same shape as the user routes above: Basic Auth at the edge, CSRF
# on writes, then one authenticated call to the worker, which owns the DB. Every
# successful write drops the redirect map so a saved URL is live immediately.
def _partners_proxy(method: str, path: str, json_body=None) -> Response:
    try:
        resp = httpx.request(method, f"{_BOT_BASE}{path}", headers=_auth_headers(),
                             json=json_body, timeout=8)
    except Exception as e:
        logger.warning("stats backend unavailable for partners route: %s", _safe_err(e))
        return _backend_error_json()
    if method != "GET" and resp.status_code < 400:
        _invalidate_partner_targets()
    return Response(resp.text, mimetype="application/json", status=resp.status_code)


@app.route("/api/partners")
@require_auth
def api_partners():
    return _partners_proxy("GET", "/partners")


@app.route("/api/partners", methods=["POST"])
@require_auth
def api_partners_create():
    if not csrf_ok():
        logger.warning("partner create rejected: CSRF check failed")
        return Response("CSRF check failed", 403)
    return _partners_proxy("POST", "/partners", request.get_json(silent=True) or {})


@app.route("/api/partners/<int:pid>", methods=["PATCH"])
@require_auth
def api_partners_update(pid: int):
    if not csrf_ok():
        logger.warning("partner update rejected: CSRF check failed")
        return Response("CSRF check failed", 403)
    return _partners_proxy("PATCH", f"/partners/{pid}", request.get_json(silent=True) or {})


@app.route("/api/partners/<int:pid>", methods=["DELETE"])
@require_auth
def api_partners_archive(pid: int):
    if not csrf_ok():
        logger.warning("partner archive rejected: CSRF check failed")
        return Response("CSRF check failed", 403)
    return _partners_proxy("DELETE", f"/partners/{pid}")


@app.route("/api/partners/<int:pid>/promo", methods=["DELETE"])
@require_auth
def api_promo_archive(pid: int):
    if not csrf_ok():
        logger.warning("promo archive rejected: CSRF check failed")
        return Response("CSRF check failed", 403)
    return _partners_proxy("DELETE", f"/partners/{pid}/promo")


@app.route("/api/partners/<int:pid>/promo/pool", methods=["POST"])
@require_auth
def api_promo_pool_import(pid: int):
    if not csrf_ok():
        logger.warning("promo pool import rejected: CSRF check failed")
        return Response("CSRF check failed", 403)
    return _partners_proxy("POST", f"/partners/{pid}/promo/pool",
                           request.get_json(silent=True) or {})


@app.route("/api/partners/<int:pid>/promo/pool", methods=["DELETE"])
@require_auth
def api_promo_pool_clear(pid: int):
    if not csrf_ok():
        logger.warning("promo pool clear rejected: CSRF check failed")
        return Response("CSRF check failed", 403)
    return _partners_proxy("DELETE", f"/partners/{pid}/promo/pool")


@app.route("/api/promo/archive", methods=["POST"])
@require_auth
def api_promo_archive_by_name():
    """Archive a campaign that has no partner row to address it by — including
    the unnamed one a legacy migration left behind."""
    if not csrf_ok():
        logger.warning("orphan promo archive rejected: CSRF check failed")
        return Response("CSRF check failed", 403)
    return _partners_proxy("POST", "/promo/archive",
                           request.get_json(silent=True) or {})


@app.route("/partners")
@require_auth
def partners_page():
    return render_template_string(PARTNERS_TEMPLATE, csrf=csrf_token())


@app.route("/users")
@require_auth
def users_page():
    return render_template_string(USERS_TEMPLATE, csrf=csrf_token())


PARTNERS_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proqnozai — Партнёры и промокоды</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--border:#2a2d3a;--accent:#6c63ff;--green:#22c55e;--red:#ef4444;--yellow:#f59e0b;--text:#e2e8f0;--muted:#94a3b8;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;}
header{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;}
.logo h1{font-size:17px;color:var(--accent);}
.btn{background:none;border:1px solid var(--border);color:var(--text);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px;text-decoration:none;display:inline-block;}
.btn:hover{border-color:var(--accent);color:var(--accent);}
.btn:disabled{opacity:.5;cursor:not-allowed;}
.btn-primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600;}
.btn-danger{border-color:var(--red);color:var(--red);}
.container{max-width:920px;margin:28px auto;padding:0 20px;}
.toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;gap:12px;flex-wrap:wrap;}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:14px;}
.card.off{opacity:.62;}
.row1{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;}
.pname{font-size:16px;font-weight:700;}
.badge{padding:3px 10px;border-radius:99px;font-size:11px;font-weight:700;white-space:nowrap;}
.b-on{background:#14532d;color:var(--green);}
.b-off{background:var(--border);color:var(--muted);}
.b-warn{background:#452c0a;color:var(--yellow);}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-top:14px;}
.f-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;}
.f-val{font-size:13px;word-break:break-all;}
.actions{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap;}
label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin:12px 0 5px;}
input,textarea{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px 12px;font-size:14px;outline:none;font-family:inherit;}
input:focus,textarea:focus{border-color:var(--accent);}
textarea{resize:vertical;min-height:120px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;}
.pool-note{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-top:12px;font-size:12px;color:var(--muted);}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
@media(max-width:560px){.grid2{grid-template-columns:1fr;}}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:16px;z-index:50;}
.modal-bg.open{display:flex;}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:22px;max-width:520px;width:100%;max-height:90vh;overflow:auto;}
.modal h2{font-size:16px;margin-bottom:6px;}
.msg{margin-top:14px;padding:10px 12px;border-radius:8px;font-size:13px;display:none;}
.msg.err{display:block;background:#450a0a;color:#fca5a5;}
.msg.ok{display:block;background:#14532d;color:#86efac;}
.hint{color:var(--muted);text-align:center;padding:40px;}
.muted{color:var(--muted);}
.chk{display:flex;align-items:center;gap:8px;margin-top:14px;}
.chk input{width:auto;}
.chk span{font-size:13px;color:var(--text);}
</style>
</head>
<body>
<header>
  <div class="logo"><h1>⚽ Proqnozai — Партнёры и промокоды</h1></div>
  <div>
    <a href="/" class="btn">📊 Статистика</a>
    <a href="/users" class="btn">👥 Пользователи</a>
    <a href="/broadcast" class="btn">📢 Рассылка</a>
  </div>
</header>
<div class="container">
  <div class="toolbar">
    <div class="muted">Изменения применяются сразу — перезапуск бота не нужен.</div>
    <button class="btn btn-primary" onclick="openAdd()">+ Добавить партнёра</button>
  </div>
  <div id="list"><div class="hint">Загрузка...</div></div>
</div>

<div class="modal-bg" id="modal">
  <div class="modal">
    <h2 id="m-title">Партнёр</h2>
    <div class="muted" style="font-size:12px">Промокод необязателен — оставьте пустым, если его нет.</div>
    <input type="hidden" id="m-id">
    <label>Название</label>
    <input id="m-name" maxlength="64" placeholder="Mostbet">
    <label>URL</label>
    <input id="m-url" maxlength="500" placeholder="https://...">
    <div class="grid2" id="m-shared-fields">
      <div>
        <label>Промокод</label>
        <input id="m-code" maxlength="64" placeholder="PROQNOZ">
      </div>
      <div>
        <label>Лимит выдач</label>
        <input id="m-limit" type="number" min="0" step="1" placeholder="1000">
      </div>
    </div>
    <div class="pool-note" id="m-pool-note" style="display:none">
      У партнёра загружен пул одноразовых кодов — код и лимит берутся из него.
      Управлять списком можно кнопкой «Загрузить коды» на карточке.
    </div>
    <div class="chk"><input type="checkbox" id="m-active" checked><span>Партнёр активен (виден в боте)</span></div>
    <div class="chk"><input type="checkbox" id="m-promo-active" checked><span>Промокод активен (выдаётся)</span></div>
    <div class="msg" id="m-msg"></div>
    <div class="actions">
      <button class="btn btn-primary" id="m-save" onclick="save()">Сохранить</button>
      <button class="btn" onclick="closeModal()">Отмена</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="pool-modal">
  <div class="modal">
    <h2 id="pool-title">Загрузить коды</h2>
    <div class="muted" style="font-size:12px">Список от партнёра: по одному коду в строке
      (или через запятую). Каждый код — на одну активацию. Повторная загрузка
      добавляет только новые коды, дубли пропускаются.</div>
    <input type="hidden" id="pool-id">
    <label>Коды</label>
    <textarea id="pool-codes" rows="10" placeholder="MB-0001&#10;MB-0002&#10;MB-0003"></textarea>
    <div class="msg" id="pool-msg"></div>
    <div class="actions">
      <button class="btn btn-primary" id="pool-save" onclick="importPool()">Загрузить</button>
      <button class="btn" onclick="closePool()">Отмена</button>
    </div>
  </div>
</div>

<script>
const CSRF = '{{ csrf }}';
let CACHE = [];
let ORPHANS = [];

function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function load(){
  const box = document.getElementById('list');
  try{
    const r = await fetch('/api/partners');
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    CACHE = (data.partners || []).filter(p => !p.is_archived);
    render(CACHE, data.orphan_promos || []);
  }catch(e){
    box.innerHTML = '<div class="hint">Не удалось загрузить: ' + esc(e.message) + '</div>';
  }
}

function render(rows, orphans){
  const box = document.getElementById('list');
  ORPHANS = orphans || [];
  if(!rows.length){
    box.innerHTML = '<div class="hint">Партнёров пока нет. Добавьте первого — он сразу появится в боте.</div>';
    return;
  }
  let html = '';
  for(const p of rows){
    const pr = p.promo;
    const isPool = !!pr && pr.mode === 'pool';
    const usage = pr ? (pr.claimed + ' / ' + pr.max_uses) : '—';
    const exhausted = pr && pr.available === 0;
    const codeCell = !pr ? '<span class="muted">нет</span>'
      : (isPool ? 'пул: ' + esc(pr.max_uses) + ' кодов по 1 активации'
                : esc(pr.code));
    let promoBadge = '';
    if(pr && !pr.is_active) promoBadge = '<span class="badge b-off">промокод выключен</span>';
    else if(exhausted) promoBadge = '<span class="badge b-warn">' + (isPool ? 'коды закончились' : 'лимит исчерпан') + '</span>';
    html += '<div class="card' + (p.is_active ? '' : ' off') + '">'
      + '<div class="row1"><div class="pname">' + esc(p.name) + '</div>'
      + '<div style="display:flex;gap:6px;align-items:center">' + promoBadge
      + '<span class="badge ' + (p.is_active ? 'b-on">● Активен' : 'b-off">○ Выключен') + '</span></div></div>'
      + '<div class="fields">'
      + '<div><div class="f-label">URL</div><div class="f-val">' + esc(p.url) + '</div></div>'
      + '<div><div class="f-label">Промокод</div><div class="f-val">' + codeCell + '</div></div>'
      + '<div><div class="f-label">' + (isPool ? 'Выдано / всего' : 'Выдано / лимит')
      + '</div><div class="f-val">' + esc(usage)
      + (pr ? ' <span class="muted">(свободно ' + esc(pr.available) + ')</span>' : '') + '</div></div>'
      + '<div><div class="f-label">Клики (30д)</div><div class="f-val">' + esc(p.clicks || 0) + '</div></div>'
      + '<div><div class="f-label">Обновлён</div><div class="f-val muted">' + esc(p.updated_at || '') + '</div></div>'
      + '</div><div class="actions">'
      + '<button class="btn" onclick="openEdit(' + p.id + ')">✏️ Изменить</button>'
      + '<button class="btn" onclick="toggle(' + p.id + ',' + (p.is_active ? 'false' : 'true') + ')">'
      + (p.is_active ? '⏸ Выключить' : '▶️ Включить') + '</button>'
      + '<button class="btn" onclick="copyUrl(' + p.id + ')">🔗 Копировать URL</button>'
      + (pr && !isPool ? '<button class="btn" onclick="copyCode(' + p.id + ')">🎁 Копировать код</button>' : '')
      + (!pr || isPool ? '<button class="btn" onclick="openPool(' + p.id + ')">📥 Загрузить коды</button>' : '')
      + (isPool ? '<button class="btn btn-danger" onclick="clearPool(' + p.id + ')">Убрать невыданные</button>' : '')
      + (pr ? '<button class="btn btn-danger" onclick="archivePromo(' + p.id + ')">Удалить промокод</button>' : '')
      + '<button class="btn btn-danger" onclick="archivePartner(' + p.id + ')">🗑 В архив</button>'
      + '</div></div>';
  }
  for(let oi = 0; oi < ORPHANS.length; oi++){
    const o = ORPHANS[oi];
    const named = !!o.partner;
    const title = named ? esc(o.partner) : 'Без названия';
    const what = o.mode === 'pool' ? 'пул: ' + esc(o.max_uses) + ' кодов' : esc(o.code);
    html += '<div class="card off"><div class="row1"><div class="pname">' + title
      + '</div><span class="badge b-warn">промокод без партнёра</span></div>'
      + '<div class="fields"><div><div class="f-label">Промокод</div><div class="f-val">' + what
      + '</div></div><div><div class="f-label">Выдано / лимит</div><div class="f-val">'
      + esc(o.claimed + ' / ' + o.max_uses) + '</div></div></div>'
      + '<div class="muted" style="margin-top:12px;font-size:12px">'
      + (named
          ? 'Выдаётся пользователям, но партнёра с таким названием нет. Добавьте его, чтобы управлять кодом на карточке партнёра.'
          : 'Досталась от старой версии: у кампании нет названия, поэтому она не привязана ни к одной карточке. Пользователям она выдаётся.')
      + '</div><div class="actions">'
      + '<button class="btn btn-danger" onclick="archiveOrphan(' + oi + ')">⏸ Отключить</button>'
      + '</div></div>';
  }
  box.innerHTML = html;
}

function byId(id){ return CACHE.find(p => p.id === id); }

function setPoolMode(on){
  document.getElementById('m-shared-fields').style.display = on ? 'none' : '';
  document.getElementById('m-pool-note').style.display = on ? '' : 'none';
}

function openAdd(){
  setPoolMode(false);
  document.getElementById('m-title').textContent = 'Новый партнёр';
  document.getElementById('m-id').value = '';
  document.getElementById('m-name').value = '';
  document.getElementById('m-url').value = '';
  document.getElementById('m-code').value = '';
  document.getElementById('m-limit').value = '';
  document.getElementById('m-active').checked = true;
  document.getElementById('m-promo-active').checked = true;
  showMsg('', '');
  document.getElementById('modal').classList.add('open');
}

function openEdit(id){
  const p = byId(id); if(!p) return;
  setPoolMode(!!p.promo && p.promo.mode === 'pool');
  document.getElementById('m-title').textContent = 'Партнёр: ' + p.name;
  document.getElementById('m-id').value = p.id;
  document.getElementById('m-name').value = p.name;
  document.getElementById('m-url').value = p.url;
  const pooled = !!p.promo && p.promo.mode === 'pool';
  document.getElementById('m-code').value = (p.promo && !pooled) ? p.promo.code : '';
  document.getElementById('m-limit').value = (p.promo && !pooled) ? p.promo.max_uses : '';
  document.getElementById('m-active').checked = !!p.is_active;
  document.getElementById('m-promo-active').checked = p.promo ? !!p.promo.is_active : true;
  showMsg('', '');
  document.getElementById('modal').classList.add('open');
}

function closeModal(){ document.getElementById('modal').classList.remove('open'); }

function showMsg(text, kind){
  const el = document.getElementById('m-msg');
  el.className = 'msg' + (kind ? ' ' + kind : '');
  el.textContent = text;
}

async function save(){
  const btn = document.getElementById('m-save');
  const id = document.getElementById('m-id').value;
  const existing = id ? byId(Number(id)) : null;
  const isPool = !!existing && !!existing.promo && existing.promo.mode === 'pool';
  const code = document.getElementById('m-code').value.trim();
  const limitRaw = document.getElementById('m-limit').value.trim();
  const body = {
    name: document.getElementById('m-name').value.trim(),
    url: document.getElementById('m-url').value.trim(),
    is_active: document.getElementById('m-active').checked,
  };
  if(isPool){
    // The code list and its size come from the pool; only the on/off switch
    // is editable here, and sending promo_code would be refused by the worker.
    body.promo_active = document.getElementById('m-promo-active').checked;
  }else if(code){
    body.promo_code = code;
    body.promo_limit = limitRaw === '' ? 0 : Number(limitRaw);
    body.promo_active = document.getElementById('m-promo-active').checked;
  }
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Сохранение...';
  showMsg('', '');
  try{
    const r = await fetch('/api/partners' + (id ? '/' + id : ''), {
      method: id ? 'PATCH' : 'POST',
      headers: {'Content-Type':'application/json','X-CSRF-Token':CSRF},
      body: JSON.stringify(body),
    });
    if(!r.ok){
      let detail = 'HTTP ' + r.status;
      try{ const j = await r.json(); if(j.error) detail = j.error; }catch(_){}
      showMsg('Не сохранено: ' + detail, 'err');
      return;
    }
    showMsg('Сохранено.', 'ok');
    await load();
    setTimeout(closeModal, 400);
  }catch(e){
    showMsg('Не сохранено: ' + e.message, 'err');
  }finally{
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function send(method, path){
  const r = await fetch(path, {method: method, headers:{'X-CSRF-Token':CSRF}});
  if(!r.ok) alert('Не выполнено: HTTP ' + r.status);
  await load();
}

async function toggle(id, active){
  const r = await fetch('/api/partners/' + id, {
    method:'PATCH', headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},
    body: JSON.stringify({is_active: active}),
  });
  if(!r.ok) alert('Не выполнено: HTTP ' + r.status);
  await load();
}

function archivePartner(id){
  const p = byId(id); if(!p) return;
  if(!confirm('Отправить «' + p.name + '» в архив? Кнопка исчезнет из бота. История кликов и выданные промокоды сохранятся.')) return;
  send('DELETE', '/api/partners/' + id);
}

function archivePromo(id){
  const p = byId(id); if(!p) return;
  if(!confirm('Удалить промокод партнёра «' + p.name + '»? Уже выданные коды у пользователей останутся.')) return;
  send('DELETE', '/api/partners/' + id + '/promo');
}

function openPool(id){
  const p = byId(id); if(!p) return;
  document.getElementById('pool-title').textContent = 'Коды для: ' + p.name;
  document.getElementById('pool-id').value = p.id;
  document.getElementById('pool-codes').value = '';
  poolMsg('', '');
  document.getElementById('pool-modal').classList.add('open');
}

function closePool(){ document.getElementById('pool-modal').classList.remove('open'); }

function poolMsg(text, kind){
  const el = document.getElementById('pool-msg');
  el.className = 'msg' + (kind ? ' ' + kind : '');
  el.textContent = text;
}

async function importPool(){
  const btn = document.getElementById('pool-save');
  const id = document.getElementById('pool-id').value;
  const codes = document.getElementById('pool-codes').value;
  if(!codes.trim()){ poolMsg('Вставьте хотя бы один код.', 'err'); return; }
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Загрузка...';
  poolMsg('', '');
  try{
    const r = await fetch('/api/partners/' + id + '/promo/pool', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},
      body: JSON.stringify({codes: codes}),
    });
    let j = {};
    try{ j = await r.json(); }catch(_){}
    if(!r.ok){ poolMsg('Не загружено: ' + (j.error || ('HTTP ' + r.status)), 'err'); return; }
    poolMsg('Добавлено ' + j.added + ', пропущено дублей ' + j.duplicates
            + '. Всего в пуле ' + j.total + ', свободно ' + j.available + '.', 'ok');
    document.getElementById('pool-codes').value = '';
    await load();
  }catch(e){
    poolMsg('Не загружено: ' + e.message, 'err');
  }finally{
    btn.disabled = false;
    btn.textContent = original;
  }
}

function clearPool(id){
  const p = byId(id); if(!p) return;
  if(!confirm('Убрать невыданные коды партнёра «' + p.name + '»? Уже выданные коды у пользователей останутся.')) return;
  send('DELETE', '/api/partners/' + id + '/promo/pool');
}

async function archiveOrphan(index){
  const o = ORPHANS[index];
  if(!o) return;
  const name = o.partner;
  const shown = name || 'Без названия';
  if(!confirm('Отключить кампанию «' + shown + '»? Она перестанет выдаваться. Уже выданные коды у пользователей останутся.')) return;
  const r = await fetch('/api/promo/archive', {
    method:'POST',
    headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},
    body: JSON.stringify({partner: name}),
  });
  if(!r.ok) alert('Не выполнено: HTTP ' + r.status);
  await load();
}

function copyText(text){
  if(navigator.clipboard) navigator.clipboard.writeText(text);
  else window.prompt('Скопируйте:', text);
}
function copyUrl(id){ const p = byId(id); if(p) copyText(p.url); }
function copyCode(id){ const p = byId(id); if(p && p.promo) copyText(p.promo.code); }

load();
</script>
</body>
</html>
"""


USERS_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proqnozai — Пользователи</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--border:#2a2d3a;--accent:#6c63ff;--green:#22c55e;--red:#ef4444;--text:#e2e8f0;--muted:#94a3b8;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;}
header{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;justify-content:space-between;}
.logo h1{font-size:17px;color:var(--accent);}
.btn{background:none;border:1px solid var(--border);color:var(--text);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px;text-decoration:none;display:inline-block;}
.btn:hover{border-color:var(--accent);color:var(--accent);}
.container{max-width:980px;margin:32px auto;padding:0 20px;}
.search{display:flex;gap:10px;margin-bottom:24px;}
input{flex:1;background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:11px 14px;font-size:14px;outline:none;}
input:focus{border-color:var(--accent);}
.btn-primary{background:var(--accent);color:#fff;border-color:var(--accent);padding:11px 22px;font-weight:600;}
table{width:100%;border-collapse:collapse;background:var(--bg2);border:1px solid var(--border);border-radius:12px;overflow:hidden;}
th{color:var(--muted);font-size:11px;text-transform:uppercase;padding:12px;text-align:left;border-bottom:1px solid var(--border);}
td{padding:12px;border-bottom:1px solid var(--border);font-size:13px;}
tr:last-child td{border-bottom:none;}
.badge{padding:3px 9px;border-radius:99px;font-size:11px;font-weight:700;}
.b-ok{background:#14532d;color:var(--green);}
.b-blk{background:#450a0a;color:var(--red);}
.act{cursor:pointer;border:none;border-radius:6px;padding:6px 12px;font-size:12px;font-weight:600;color:#fff;}
.a-block{background:var(--red);}
.a-unblock{background:var(--green);}
.muted{color:var(--muted);}
.hint{color:var(--muted);text-align:center;padding:30px;}
</style>
</head>
<body>
<header>
  <div class="logo"><h1>⚽ Proqnozai — Пользователи</h1></div>
  <div>
    <a href="/" class="btn">📊 Статистика</a>
    <a href="/partners" class="btn">🤝 Партнёры</a>
    <a href="/broadcast" class="btn">📢 Рассылка</a>
  </div>
</header>
<div class="container">
  <div class="search">
    <input id="q" placeholder="ID, @username или имя..." onkeydown="if(event.key==='Enter')doSearch()">
    <button class="btn btn-primary" onclick="doSearch()">🔍 Найти</button>
  </div>
  <div id="result"><div class="hint">Введите запрос для поиска пользователей.</div></div>
</div>
<script>
async function doSearch(){
  const q = document.getElementById('q').value.trim();
  const box = document.getElementById('result');
  if(!q){ box.innerHTML = '<div class="hint">Введите запрос.</div>'; return; }
  box.innerHTML = '<div class="hint">Поиск...</div>';
  try{
    const r = await fetch('/api/users/search?q=' + encodeURIComponent(q));
    const data = await r.json();
    const users = data.users || [];
    if(!users.length){ box.innerHTML = '<div class="hint">Ничего не найдено.</div>'; return; }
    let html = '<table><tr><th>ID</th><th>Имя</th><th>Username</th><th>Язык</th><th>Запросов</th><th>Статус</th><th></th></tr>';
    for(const u of users){
      const blocked = u.is_blocked;
      html += '<tr>'
        + '<td class="muted">'+u.user_id+'</td>'
        + '<td><strong>'+(u.display_name||'—')+'</strong></td>'
        + '<td class="muted">@'+(u.username||'-')+'</td>'
        + '<td>'+(u.lang||'')+'</td>'
        + '<td>'+(u.total_requests||0)+'</td>'
        + '<td>'+(blocked?'<span class="badge b-blk">🚫 Блок</span>':'<span class="badge b-ok">✅ Активен</span>')+'</td>'
        + '<td><button class="act '+(blocked?'a-unblock':'a-block')+'" onclick="toggleBlock('+u.user_id+','+(blocked?0:1)+')">'+(blocked?'Разблокировать':'Заблокировать')+'</button></td>'
        + '</tr>';
    }
    html += '</table>';
    box.innerHTML = html;
  }catch(e){ box.innerHTML = '<div class="hint">Ошибка: '+e+'</div>'; }
}
async function toggleBlock(uid, blocked){
  try{
    await fetch('/api/users/block', {method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':'{{ csrf }}'},
      body: JSON.stringify({user_id: uid, blocked: blocked})});
    doSearch();
  }catch(e){ alert('Ошибка: '+e); }
}
</script>
</body>
</html>"""


BROADCAST_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proqnozai — Рассылка</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--border:#2a2d3a;--accent:#6c63ff;--accent2:#a78bfa;--green:#22c55e;--red:#ef4444;--text:#e2e8f0;--muted:#94a3b8;}
[data-theme="light"]{--bg:#f1f5f9;--bg2:#ffffff;--border:#e2e8f0;--accent:#6c63ff;--text:#0f172a;--muted:#64748b;}
[data-theme="ocean"]{--bg:#0c1929;--bg2:#112236;--border:#1e3a5f;--accent:#38bdf8;--text:#e0f2fe;--muted:#7dd3fc;}
[data-theme="forest"]{--bg:#0a1612;--bg2:#122218;--border:#1e3a28;--accent:#22c55e;--text:#dcfce7;--muted:#86efac;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;}
header{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;}
.logo{display:flex;align-items:center;gap:10px;}
.logo h1{font-size:17px;font-weight:700;color:var(--accent);}
.header-right{display:flex;align-items:center;gap:12px;}
.btn{background:none;border:1px solid var(--border);color:var(--text);padding:5px 10px;border-radius:6px;cursor:pointer;font-size:12px;transition:all .2s;text-decoration:none;display:inline-block;}
.btn:hover{border-color:var(--accent);color:var(--accent);}
.btn-primary{background:var(--accent);color:#fff;border-color:var(--accent);padding:10px 24px;font-size:14px;font-weight:600;}
.btn-primary:hover{background:var(--accent2);border-color:var(--accent2);color:#fff;}
.container{max-width:760px;margin:40px auto;padding:0 20px;}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.3);}
h2{font-size:20px;font-weight:700;margin-bottom:6px;}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px;}
label{display:block;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:6px;}
select,textarea{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px 12px;font-size:14px;font-family:inherit;transition:border-color .2s;outline:none;margin-bottom:20px;}
select:focus,textarea:focus{border-color:var(--accent);}
textarea{min-height:160px;resize:vertical;}
.char-count{text-align:right;font-size:11px;color:var(--muted);margin-top:-16px;margin-bottom:20px;}
.alert{padding:16px 20px;border-radius:8px;margin-bottom:24px;font-size:14px;}
.alert-success{background:#14532d;border:1px solid #16a34a;color:#86efac;}
.alert-error{background:#450a0a;border:1px solid #b91c1c;color:#fca5a5;}
.preview-box{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:14px;font-size:13px;color:var(--muted);min-height:60px;white-space:pre-wrap;margin-bottom:20px;}
.seg-count{display:inline-block;background:rgba(108,99,255,.15);color:var(--accent);border-radius:6px;padding:2px 8px;font-size:12px;font-weight:700;margin-left:8px;}
.divider{border:none;border-top:1px solid var(--border);margin:24px 0;}
</style>
</head>
<body data-theme="dark">
<header>
  <div class="logo">
    <span style="font-size:22px">⚽</span>
    <h1>Proqnozai Bot</h1>
  </div>
  <div class="header-right">
    <a href="/" class="btn">📊 Статистика</a>
    <a href="/users" class="btn">👥 Пользователи</a>
    <a href="/broadcast" class="btn" style="border-color:var(--accent);color:var(--accent)">📢 Рассылка</a>
    <button class="btn" onclick="setTheme('dark')">🌙</button>
    <button class="btn" onclick="setTheme('light')">☀️</button>
    <button class="btn" onclick="setTheme('ocean')">🌊</button>
    <button class="btn" onclick="setTheme('forest')">🌿</button>
  </div>
</header>

<div class="container">
  <div class="card">
    <h2>📢 Рассылка</h2>
    <p class="sub">Отправить сообщение сегменту пользователей через Telegram-бота</p>

    {% if result %}
    <div class="alert alert-{{ 'success' if result.started > 0 else 'error' }}">
      {% if result.started > 0 %}
      🚀 Рассылка запущена для <strong>{{ result.started }}</strong> чел.
      Отправка идёт в фоне (~20 сообщений/сек). Прогресс можно отслеживать в логах бота.
      {% else %}
      ❌ Не удалось запустить: {{ result.error or 'неизвестная ошибка' }}
      {% endif %}
    </div>
    {% endif %}

    <div id="bcastProgress" style="display:none;margin-bottom:24px;">
      <label>Прогресс рассылки</label>
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;height:14px;overflow:hidden;margin:6px 0;">
        <div id="bcastBar" style="height:14px;width:0%;background:var(--accent);transition:width .4s;"></div>
      </div>
      <div id="bcastText" class="sub" style="margin:0;"></div>
    </div>

    <form method="POST" action="/broadcast" onsubmit="return confirmSend()">
      <input type="hidden" name="csrf" value="{{ csrf }}">
      <label for="segment">Аудитория</label>
      <select name="segment" id="segment" onchange="updateCount()">
        <option value="all">👥 Все активные пользователи</option>
        <optgroup label="По языку">
          <option value="lang:az">🇦🇿 Azərbaycan</option>
          <option value="lang:ru">🇷🇺 Русский</option>
          <option value="lang:en">🇬🇧 English</option>
          <option value="lang:tr">🇹🇷 Türkçe</option>
          <option value="lang:kz">🇰🇿 Қазақша</option>
          <option value="lang:uz">🇺🇿 O'zbek</option>
          <option value="lang:ar">🇸🇦 العربية</option>
        </optgroup>
        <optgroup label="По активности">
          <option value="act:active">🟢 Активные (≤7 дней)</option>
          <option value="act:churn">🟡 Отток (7–30 дней)</option>
          <option value="act:sleep">🔴 Спящие (>30 дней)</option>
        </optgroup>
      </select>

      <label for="text">Текст сообщения</label>
      <textarea name="text" id="text" placeholder="Введите текст рассылки..." oninput="updateCount()"
                maxlength="4096">{{ prefill or '' }}</textarea>
      <div class="char-count"><span id="charCount">0</span> / 4096 символов</div>

      <label>Превью</label>
      <div class="preview-box" id="preview">Начните вводить текст...</div>

      <hr class="divider">
      <button type="submit" class="btn btn-primary">📤 Отправить рассылку</button>
      <span style="color:var(--muted);font-size:12px;margin-left:12px;">⚠️ Действие необратимо</span>
    </form>
  </div>

  <div style="margin-top:16px;text-align:center;color:var(--muted);font-size:12px;">
    Рассылка отправляется напрямую через бот · Лимит Telegram: 30 сообщений/сек
  </div>
</div>

<script>
(function(){
  const saved = localStorage.getItem('theme') || 'dark';
  document.body.setAttribute('data-theme', saved);
})();
function setTheme(t){
  document.body.setAttribute('data-theme',t);
  localStorage.setItem('theme',t);
}
function updateCount(){
  const t = document.getElementById('text').value;
  document.getElementById('charCount').textContent = t.length;
  document.getElementById('preview').textContent = t || 'Начните вводить текст...';
}
function confirmSend(){
  const seg = document.getElementById('segment').options[document.getElementById('segment').selectedIndex].text;
  const len = document.getElementById('text').value.trim().length;
  if(!len){ alert('Введите текст'); return false; }
  return confirm('Отправить рассылку?\nАудитория: ' + seg + '\n\nЭто действие необратимо.');
}
updateCount();

// ── Live broadcast progress ──────────────────────────────────────────────────
async function pollBroadcast(){
  try{
    const r = await fetch('/api/broadcast/status'); if(!r.ok) return;
    const s = await r.json();
    const box = document.getElementById('bcastProgress');
    const total = s.total||0, done = (s.ok||0)+(s.fail||0);
    if(s.running || (s.done && total)){
      box.style.display = 'block';
      const pct = total ? Math.round(done/total*100) : 0;
      document.getElementById('bcastBar').style.width = pct + '%';
      document.getElementById('bcastText').textContent =
        (s.running ? '⏳ Идёт рассылка' : '✅ Завершено') +
        `: ${done}/${total} · ✅ ${s.ok||0} · ❌ ${s.fail||0}`;
    }
  }catch(e){}
}
setInterval(pollBroadcast, 2000);
pollBroadcast();
</script>
</body>
</html>"""


@app.route("/broadcast", methods=["GET", "POST"])
@require_auth
def broadcast():
    result = None
    prefill = ""

    if request.method == "POST":
        if not csrf_ok():
            # A cross-site POST replaying the admin's Basic Auth credentials.
            logger.warning("broadcast rejected: CSRF check failed")
            return Response("CSRF check failed", 403)
        text    = (request.form.get("text") or "").strip()
        segment = request.form.get("segment", "all")
        prefill = text

        if not text:
            result = {"started": 0, "error": "Пустой текст"}
        else:
            try:
                resp = httpx.post(
                    BROADCAST_URL, headers=_auth_headers(),
                    json={"text": text, "segment": segment},
                    timeout=15,
                )
                data = resp.json()
                if resp.status_code == 200:
                    result = data  # {"started": N}
                elif resp.status_code == 409:
                    result = {"started": 0, "error": "Рассылка уже выполняется, дождитесь её окончания."}
                else:
                    result = {"started": 0, "error": data.get("detail", f"HTTP {resp.status_code}")}
            except Exception as e:
                logger.warning("broadcast backend unavailable: %s", _safe_err(e))
                result = {"started": 0, "error": "Сервис недоступен, попробуйте позже."}

    return render_template_string(BROADCAST_TEMPLATE, result=result, prefill=prefill,
                                  csrf=csrf_token())


@app.route("/health")
def health():
    return "ok"


def _port() -> int:
    """Resolve the bind port from the platform-provided PORT (Railway/Heroku),
    defaulting to 5000 for local runs."""
    try:
        return int(os.environ.get("PORT", "5000"))
    except ValueError:
        return 5000


if __name__ == "__main__":
    # Bind 0.0.0.0 so the platform can route external traffic to the web process.
    app.run(host="0.0.0.0", port=_port(), debug=False)
