"""
Proqnozai Bot Dashboard — Flask web process.
Auth: HTTP Basic Auth (login: admin, password: DASHBOARD_TOKEN)
Stats source: bot's internal stats server (stats_server.py via Railway private network)

The web process has no database of its own: every number and every action goes
to the worker over HTTP.
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


# ─── Shared look ──────────────────────────────────────────────────────────────
# One stylesheet and one header for all three pages. They used to carry three
# divergent copies, so every visual fix had to be made three times and never
# was — the users page had already drifted to a different button, table and
# spacing scale than the dashboard it links to.
BASE_CSS = r"""
:root{
  --bg:#0d0f16; --surface:#151824; --surface2:#1b1f2e; --border:#272b3b;
  --accent:#6c63ff; --accent2:#a78bfa;
  --ok:#22c55e; --bad:#ef4444; --warn:#f59e0b; --info:#38bdf8;
  --text:#e7ecf3; --muted:#8e9bb3;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.6);
  --radius:14px;
}
[data-theme="light"]{
  --bg:#f5f7fb; --surface:#ffffff; --surface2:#f2f5fa; --border:#e3e8f0;
  --accent:#5b53f0; --accent2:#7c3aed;
  --text:#111827; --muted:#5f6b80;
  --shadow:0 1px 2px rgba(16,24,40,.06), 0 10px 24px -14px rgba(16,24,40,.25);
}
[data-theme="ocean"]{
  --bg:#08131f; --surface:#0f1e30; --surface2:#132639; --border:#1e3a5f;
  --accent:#38bdf8; --accent2:#0ea5e9; --text:#e0f2fe; --muted:#7fb6d8;
}
[data-theme="forest"]{
  --bg:#08130f; --surface:#101f18; --surface2:#152a20; --border:#1e3a28;
  --accent:#34d399; --accent2:#16a34a; --text:#dcfce7; --muted:#86b39a;
}

*{box-sizing:border-box;margin:0;padding:0;}
html{-webkit-text-size-adjust:100%;}
body{
  background:var(--bg); color:var(--text); font-size:14px; line-height:1.5;
  font-family:system-ui,-apple-system,'Segoe UI',Inter,Roboto,sans-serif;
  font-variant-numeric:tabular-nums;
  transition:background .2s,color .2s;
}
a{color:inherit;}

/* ── Header ── */
header{
  background:color-mix(in srgb, var(--surface) 88%, transparent);
  border-bottom:1px solid var(--border); padding:10px 20px;
  display:flex; align-items:center; justify-content:space-between; gap:16px;
  position:sticky; top:0; z-index:100; backdrop-filter:blur(10px); flex-wrap:wrap;
}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;}
.brand .mark{
  width:30px;height:30px;border-radius:9px;display:grid;place-items:center;font-size:16px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
}
.brand h1{font-size:15px;font-weight:700;letter-spacing:-.01em;}
.brand small{display:block;color:var(--muted);font-size:11px;font-weight:500;}
.nav{display:flex;gap:4px;background:var(--surface2);padding:4px;border-radius:11px;border:1px solid var(--border);}
.nav a{
  padding:6px 12px;border-radius:8px;font-size:13px;font-weight:600;
  color:var(--muted);text-decoration:none;transition:.15s;white-space:nowrap;
}
.nav a:hover{color:var(--text);}
.nav a.on{background:var(--surface);color:var(--text);box-shadow:var(--shadow);}
.tools{display:flex;align-items:center;gap:8px;}
.stamp{color:var(--muted);font-size:11px;background:var(--surface2);border:1px solid var(--border);
  padding:5px 10px;border-radius:99px;white-space:nowrap;}
.themes{display:flex;gap:2px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:3px;}
.themes button{background:none;border:none;cursor:pointer;font-size:13px;padding:4px 7px;border-radius:7px;line-height:1;}
.themes button.on{background:var(--surface);box-shadow:var(--shadow);}

/* ── Layout ── */
.container{max-width:1320px;margin:0 auto;padding:22px 20px 40px;}
.sec{display:flex;align-items:center;gap:10px;margin:26px 0 12px;}
.sec h2{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);}
.sec::after{content:'';flex:1;height:1px;background:var(--border);}
.grid{display:grid;gap:12px;}
.g2{grid-template-columns:repeat(2,1fr);} .g3{grid-template-columns:repeat(3,1fr);}
.g4{grid-template-columns:repeat(4,1fr);} .g5{grid-template-columns:repeat(5,1fr);}
.span2{grid-column:span 2;} .span3{grid-column:span 3;}
@media(max-width:1100px){.g5,.g4{grid-template-columns:repeat(2,1fr);}}
@media(max-width:820px){.g5,.g4,.g3,.g2{grid-template-columns:1fr;}.span2,.span3{grid-column:span 1;}}

/* ── Cards ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px 18px;box-shadow:var(--shadow);}
.card h3{font-size:13px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:7px;}
.label{color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;}
.value{font-size:29px;font-weight:750;letter-spacing:-.02em;line-height:1.15;margin:6px 0 2px;}
.value small{font-size:16px;font-weight:600;color:var(--muted);}
.sub{color:var(--muted);font-size:12px;}
.ok{color:var(--ok);} .bad{color:var(--bad);} .warn{color:var(--warn);}
.acc{color:var(--accent);} .info{color:var(--info);} .muted{color:var(--muted);}
.stat{position:relative;}
.stat .ico{position:absolute;right:14px;top:14px;font-size:20px;opacity:.22;}

/* ── Delta chips ── */
.delta{display:inline-flex;align-items:center;gap:3px;font-size:11px;font-weight:700;
  padding:2px 7px;border-radius:7px;vertical-align:middle;margin-left:6px;}
.d-up{background:color-mix(in srgb,var(--ok) 16%,transparent);color:var(--ok);}
.d-down{background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad);}
.d-flat{background:var(--surface2);color:var(--muted);}

/* ── Bars ── */
.bar-wrap{background:var(--surface2);border-radius:99px;height:7px;overflow:hidden;margin-top:8px;}
.bar{height:7px;border-radius:99px;transition:width .6s ease;background:linear-gradient(90deg,var(--accent),var(--accent2));}
.bar.green{background:var(--ok);} .bar.warn{background:var(--warn);}

/* ── Tables ── */
table{width:100%;border-collapse:collapse;}
th{color:var(--muted);font-weight:700;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  padding:9px 10px;text-align:left;border-bottom:1px solid var(--border);}
td{padding:9px 10px;border-bottom:1px solid var(--border);font-size:13px;}
tbody tr:last-child td,tr:last-child td{border-bottom:none;}
tr:hover td{background:color-mix(in srgb,var(--accent) 6%,transparent);}
.tbl-wrap{overflow-x:auto;}

/* ── Badges & buttons ── */
.badge{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:99px;
  font-size:11px;font-weight:700;background:var(--surface2);color:var(--muted);}
.badge.win{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok);}
.badge.lose{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad);}
.badge.lang{background:color-mix(in srgb,var(--accent) 15%,transparent);color:var(--accent);}
.btn{background:var(--surface2);border:1px solid var(--border);color:var(--text);
  padding:8px 14px;border-radius:10px;cursor:pointer;font-size:13px;font-weight:600;
  text-decoration:none;display:inline-flex;align-items:center;gap:6px;transition:.15s;font-family:inherit;}
.btn:hover{border-color:var(--accent);color:var(--accent);}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
.btn.primary:hover{background:var(--accent2);border-color:var(--accent2);color:#fff;}
.btn.danger{color:var(--bad);}
.btn.danger:hover{border-color:var(--bad);}
.btn:disabled{opacity:.5;cursor:not-allowed;}
.btn.sm{padding:5px 10px;font-size:12px;border-radius:8px;}

/* ── Forms ── */
label.f{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);margin:0 0 6px;}
input,select,textarea{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);
  border-radius:10px;padding:10px 12px;font-size:14px;font-family:inherit;outline:none;transition:border-color .15s;}
input:focus,select:focus,textarea:focus{border-color:var(--accent);}
textarea{min-height:170px;resize:vertical;line-height:1.55;}
.field{margin-bottom:16px;}
.row{display:flex;gap:10px;align-items:center;}
.chk{display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);cursor:pointer;}
.chk input{width:auto;}

/* ── Charts ── */
.chart{position:relative;height:210px;}
.chart.sm{height:165px;}
.chart-fallback{color:var(--muted);font-size:13px;padding:24px 8px;text-align:center;}

/* ── Rank pills ── */
.rank{width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:700;background:var(--surface2);color:var(--muted);}
.rank.r1{background:linear-gradient(135deg,#f59e0b,#fbbf24);color:#000;}
.rank.r2{background:linear-gradient(135deg,#94a3b8,#cbd5e1);color:#000;}
.rank.r3{background:linear-gradient(135deg,#b45309,#d97706);color:#fff;}

/* ── Misc ── */
.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ok);
  animation:pulse 2s infinite;}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 45%,transparent);}
  70%{box-shadow:0 0 0 7px transparent;}100%{box-shadow:0 0 0 0 transparent;}}
.empty{color:var(--muted);font-size:13px;padding:22px;text-align:center;}
footer{text-align:center;color:var(--muted);font-size:11px;padding:26px;border-top:1px solid var(--border);margin-top:30px;}
"""

# Nav is one string with the current page marked, so a new page is added once.
HEADER = r"""
<header>
  <div class="brand">
    <span class="mark">⚽</span>
    <div>
      <h1>Proqnozai</h1>
      <small><span class="pulse"></span> {{ subtitle }}</small>
    </div>
  </div>
  <nav class="nav">
    <a href="/" class="{{ 'on' if page=='stats' else '' }}">📊 Статистика</a>
    <a href="/users" class="{{ 'on' if page=='users' else '' }}">👥 Пользователи</a>
    <a href="/partners" class="{{ 'on' if page=='partners' else '' }}">🤝 Партнёры</a>
    <a href="/broadcast" class="{{ 'on' if page=='broadcast' else '' }}">📢 Рассылка</a>
  </nav>
  <div class="tools">
    <span class="stamp" id="stamp">{{ stamp }}</span>
    <div class="themes">
      <button data-t="dark" onclick="setTheme('dark')">🌙</button>
      <button data-t="light" onclick="setTheme('light')">☀️</button>
      <button data-t="ocean" onclick="setTheme('ocean')">🌊</button>
      <button data-t="forest" onclick="setTheme('forest')">🌿</button>
    </div>
  </div>
</header>
"""

THEME_JS = r"""
function markTheme(t){
  document.querySelectorAll('.themes button').forEach(b=>b.classList.toggle('on', b.dataset.t===t));
}
function setTheme(t){
  document.documentElement.setAttribute('data-theme',t);
  localStorage.setItem('theme',t); markTheme(t);
  if (typeof updateCharts === 'function') updateCharts();
}
(function(){
  const saved = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  document.addEventListener('DOMContentLoaded', ()=>markTheme(saved));
})();
"""


def _page(body: str, title: str) -> str:
    """Wrap page-specific markup in the shared shell."""
    return (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{title}</title>'
        "{% block head %}{% endblock %}"
        f"<style>{BASE_CSS}</style></head><body>"
        f"<script>{THEME_JS}</script>"
        f"{HEADER}{body}</body></html>"
    )


# ─── Stats page ───────────────────────────────────────────────────────────────
_STATS_BODY = r"""
<div class="container">

  <div class="sec"><h2>Ключевые показатели</h2></div>
  <div class="grid g5">
    <div class="card stat">
      <span class="ico">👥</span>
      <div class="label">Пользователи</div>
      <div class="value acc" id="k_users">{{ d.users_total }}</div>
      <div class="sub">+{{ d.users_week }} за неделю {{ chip(dl.users) }} · {{ d.users_blocked }} в блоке</div>
    </div>
    <div class="card stat">
      <span class="ico">✨</span>
      <div class="label">Новые сегодня</div>
      <div class="value ok" id="k_new">+{{ d.users_today }}</div>
      <div class="sub">неделя {{ d.users_week }} · до этого {{ d.users_prev_week }}</div>
    </div>
    <div class="card stat">
      <span class="ico">🔥</span>
      <div class="label">Активны сегодня</div>
      <div class="value" id="k_dau">{{ d.users_active_today }}</div>
      <div class="sub">вчера {{ d.users_active_yday }} {{ chip(dl.dau) }} · WAU {{ d.users_active_week }}</div>
    </div>
    <div class="card stat">
      <span class="ico">📊</span>
      <div class="label">Прогнозы</div>
      <div class="value acc" id="k_forecasts">{{ d.forecasts_real_total }}</div>
      <div class="sub">сегодня {{ d.forecasts_real_today }} · неделя {{ d.forecasts_week }} {{ chip(dl.forecasts) }}</div>
    </div>
    <div class="card stat">
      <span class="ico">🎯</span>
      <div class="label">Точность (оценки)</div>
      <div class="value {{ 'ok' if d.fb_pct >= 60 else 'warn' if d.fb_pct >= 40 else 'bad' }}" id="k_acc">{{ d.fb_pct }}%</div>
      <div class="sub">{{ d.fb_wins }} побед из {{ d.fb_total }} оценок</div>
      <div class="bar-wrap"><div class="bar {{ 'green' if d.fb_pct >= 60 else '' }}" style="width:{{ d.fb_pct }}%"></div></div>
    </div>
  </div>

  <div class="grid g4" style="margin-top:12px">
    <div class="card stat">
      <span class="ico">📨</span>
      <div class="label">Запросов всего</div>
      <div class="value" id="k_reqs">{{ d.reqs_total }}</div>
      <div class="sub">сегодня {{ d.reqs_today }} · неделя {{ d.reqs_week }} {{ chip(dl.reqs) }}</div>
    </div>
    <div class="card stat">
      <span class="ico">🔁</span>
      <div class="label">Возвращаются</div>
      <div class="value {{ 'ok' if d.repeat_pct >= 40 else 'warn' }}">{{ d.repeat_pct }}%</div>
      <div class="sub">{{ d.repeat_users }} из {{ d.users_with_activity }} что-то делали дважды</div>
      <div class="bar-wrap"><div class="bar" style="width:{{ d.repeat_pct }}%"></div></div>
    </div>
    <div class="card stat">
      <span class="ico">📡</span>
      <div class="label">Live-подписки</div>
      <div class="value warn">{{ d.live_subs }}</div>
      <div class="sub">{{ d.live_matches }} уникальных матчей</div>
    </div>
    <div class="card stat">
      <span class="ico">📢</span>
      <div class="label">Рассылки (30 дней)</div>
      <div class="value">{{ bc.ok|default(0) }}<small> доставлено</small></div>
      <div class="sub">
        {{ bc.campaigns|default(0) }} кампаний · доставка {{ bc.delivery_pct|default(0) }}%
        {% if bc.pending %}· <span class="acc">запланировано {{ bc.pending }}</span>{% endif %}
      </div>
    </div>
  </div>

  {% set f = d.funnel|default({}) %}{% set e = d.engagement|default({}) %}
  {% set fh = d.forecast_health|default({}) %}{% set fc = d.feedback_coverage|default({}) %}
  {% set pr = d.promo|default({}) %}{% set pt = d.partners|default({}) %}
  {% set ch = d.churn|default({}) %}
  {% if e %}
  <div class="sec"><h2>Продукт</h2></div>
  <div class="grid g4">
    <div class="card">
      <div class="label">DAU / WAU / MAU</div>
      <div class="value">{{ e.dau }}<small> / {{ e.wau }} / {{ e.mau }}</small></div>
      <div class="sub">Липучесть DAU/MAU: <b class="{{ 'ok' if e.stickiness >= 20 else 'warn' }}">{{ e.stickiness }}%</b></div>
    </div>
    <div class="card">
      <div class="label">Прогнозов на активного</div>
      <div class="value">{{ e.forecasts_per_wau }}</div>
      <div class="sub">за неделю на одного WAU</div>
    </div>
    <div class="card">
      <div class="label">Успешность генерации</div>
      <div class="value {{ 'ok' if fh.ok_pct|default(0) >= 95 else 'bad' }}">{{ fh.ok_pct|default(0) }}%</div>
      <div class="sub">7 дней: {{ fh.ok|default(0) }} из {{ fh.total|default(0) }} · сбоев {{ fh.failed|default(0) }}</div>
    </div>
    <div class="card">
      <div class="label">Время генерации</div>
      <div class="value">{{ (fh.p50_ms|default(0) / 1000)|round(1) }}<small> с</small></div>
      <div class="sub">медиана · среднее {{ (fh.avg_ms|default(0) / 1000)|round(1) }} с</div>
    </div>
  </div>

  <div class="grid g3" style="margin-top:12px">
    <div class="card">
      <h3>🚪 Воронка активации</h3>
      {% set base = f.started|default(1) or 1 %}
      {% for name, val in [('Пришли', f.started|default(0)), ('Зарегистрировались', f.registered|default(0)),
                           ('Прошли онбординг', f.onboarded|default(0)), ('Получили прогноз', f.forecasted|default(0))] %}
      <div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;font-size:13px">
          <span>{{ name }}</span><span><b>{{ val }}</b> <span class="muted">{{ (val / base * 100)|round|int }}%</span></span>
        </div>
        <div class="bar-wrap"><div class="bar" style="width:{{ (val / base * 100)|round|int }}%"></div></div>
      </div>
      {% endfor %}
    </div>

    <div class="card span2">
      <h3>📅 Удержание по когортам</h3>
      <div class="tbl-wrap">
      <table>
        <tr><th>Дата</th><th>Когорта</th><th>D1</th><th>D7</th><th>D30</th></tr>
        {% for r in (d.retention|default([]))[:7] %}
        <tr><td>{{ r.day }}</td><td>{{ r.size }}</td>
            {% for v in [r.d1, r.d7, r.d30] %}
            <td>{% if v is none %}<span class="muted">—</span>
                {% else %}<span class="{{ 'ok' if v >= 30 else 'warn' if v >= 10 else 'bad' }}">{{ v }}%</span>{% endif %}</td>
            {% endfor %}
        </tr>
        {% else %}
        <tr><td colspan="5" class="empty">Пока нет данных</td></tr>
        {% endfor %}
      </table>
      </div>
    </div>
  </div>

  <div class="grid g4" style="margin-top:12px">
    <div class="card">
      <h3>🤝 Партнёры (30 дней)</h3>
      <div class="value" style="font-size:24px">{{ pt.total|default(0) }}</div>
      <div class="sub" style="margin-bottom:10px">
        кликов · уникальных {{ pt.unique_users|default(0) }}
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
      <div class="sub">Трекинг кликов выключен (PARTNER_REDIRECT_BASE не задан)</div>
      {% endif %}
    </div>

    <div class="card">
      <h3>🎁 Промокоды</h3>
      {% if pr.partners %}
      <div class="value" style="font-size:24px">{{ pr.claimed }}<small> / {{ pr.max_uses }}</small></div>
      <div class="bar-wrap"><div class="bar green" style="width:{{ (pr.claimed / (pr.max_uses or 1) * 100)|round|int }}%"></div></div>
      <div class="sub" style="margin:8px 0 10px">
        получили {{ pr.users }} чел. · за неделю {{ pr.claimed_7d }} · конверсия {{ pr.conversion }}%
      </div>
      <table>
        <tr><th>Партнёр</th><th>Код</th><th>Выдано</th></tr>
        {% for c in pr.partners %}
        <tr><td>{{ c.partner or '—' }}</td><td>{{ c.code }}</td><td>{{ c.claimed }}/{{ c.max_uses }}</td></tr>
        {% endfor %}
      </table>
      {% else %}
      <div class="sub">Активных кампаний нет</div>
      {% endif %}
    </div>

    <div class="card">
      <h3>💤 Отток и оценки</h3>
      <div class="sub" style="line-height:2">
        Активны за 7 дней: <b class="ok">{{ ch.active_7d|default(0) }}</b><br>
        Молчат 7–30 дней: <b class="warn">{{ ch.silent_7_30|default(0) }}</b><br>
        Молчат больше 30: <b class="bad">{{ ch.silent_30|default(0) }}</b><br>
        Ни одного действия: <b class="muted">{{ ch.never|default(0) }}</b><br>
        Оценено прогнозов: <b>{{ fc.pct|default(0) }}%</b>
        <span class="muted">({{ fc.rated|default(0) }} из {{ fc.total|default(0) }})</span>
      </div>
    </div>

    <div class="card">
      <h3>🧭 Действия за неделю</h3>
      {% set acts = d.by_action|default([]) %}
      {% set act_total = (acts|sum(attribute=1)) or 1 %}
      {% for name, cnt in acts[:6] %}
      <div style="margin-bottom:9px">
        <div style="display:flex;justify-content:space-between;font-size:13px">
          <span class="muted">{{ name }}</span><b>{{ cnt }}</b>
        </div>
        <div class="bar-wrap"><div class="bar" style="width:{{ (cnt / act_total * 100)|round|int }}%"></div></div>
      </div>
      {% else %}
      <div class="empty">Нет событий за неделю</div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <div class="sec"><h2>Аналитика</h2></div>
  <div class="grid g3">
    <div class="card span2">
      <h3>📈 Запросы за 14 дней</h3>
      <div class="chart"><canvas id="lineChart"></canvas></div>
    </div>
    <div class="card">
      <h3>🌍 Языки</h3>
      <div class="chart"><canvas id="langChart"></canvas></div>
    </div>
    <div class="card span2">
      <h3>📊 Прогнозы по дням</h3>
      <div class="chart sm"><canvas id="barChart"></canvas></div>
    </div>
    <div class="card">
      <h3>🎯 Результаты прогнозов</h3>
      <div class="chart sm"><canvas id="feedbackChart"></canvas></div>
    </div>
    <div class="card span3">
      <h3>📈 Точность (win-rate) за 14 дней, %</h3>
      <div class="chart sm"><canvas id="winrateChart"></canvas></div>
    </div>
  </div>

  <div class="sec"><h2>Люди</h2></div>
  <div class="grid g3">
    <div class="card span2">
      <h3>🏆 Топ пользователей</h3>
      <div class="tbl-wrap">
      <table>
        <tr><th></th><th>Пользователь</th><th>ID</th><th>Запросов</th><th>Последняя активность</th></tr>
        {% for u in d.top_users %}
        <tr>
          <td><span class="rank {{ 'r1' if loop.index==1 else 'r2' if loop.index==2 else 'r3' if loop.index==3 else '' }}">{{ loop.index }}</span></td>
          <td><strong>{{ u[1] or u[2] or '—' }}</strong></td>
          <td class="muted">{{ u[0] }}</td>
          <td><b class="acc">{{ u[3] }}</b></td>
          <td class="muted">{{ (u[4] or '')[:16] }}</td>
        </tr>
        {% else %}<tr><td colspan="5" class="empty">Пока пусто</td></tr>{% endfor %}
      </table>
      </div>
    </div>
    <div class="card">
      <h3>✨ Новые пользователи</h3>
      <table>
        {% for u in d.recent_users %}
        <tr>
          <td><strong>{{ u[1] or u[2] or u[0] }}</strong></td>
          <td><span class="badge lang">{{ u[3] }}</span></td>
          <td class="muted">{{ (u[4] or '')[:16] }}</td>
        </tr>
        {% else %}<tr><td class="empty">Пока пусто</td></tr>{% endfor %}
      </table>
    </div>
  </div>

  <div class="grid g2" style="margin-top:12px">
    <div class="card">
      <h3>🕐 Последние прогнозы</h3>
      <table>
        {% for f in d.recent_forecasts %}
        <tr>
          <td><strong>{{ f[1] or f[0] }}</strong></td>
          <td class="muted">{{ (f[2] or '?')[:24] }}</td>
          <td>
            {% if f[3] == 1 %}<span class="badge win">✓ Победа</span>
            {% elif f[3] == 0 %}<span class="badge lose">✗ Проигрыш</span>
            {% else %}<span class="badge">— Нет оценки</span>{% endif %}
          </td>
        </tr>
        {% else %}<tr><td class="empty">Пока пусто</td></tr>{% endfor %}
      </table>
    </div>
    <div class="card">
      <h3>🌍 Распределение по языкам</h3>
      {% set total_lang = (d.langs|sum(attribute=1)) or 1 %}
      <table>
        {% for l in d.langs %}
        {% set pct = (l[1] / total_lang * 100)|round(1) %}
        <tr>
          <td><span class="badge lang">{{ l[0] }}</span></td>
          <td><strong>{{ l[1] }}</strong></td>
          <td class="muted">{{ pct }}%</td>
          <td style="width:45%"><div class="bar-wrap"><div class="bar" style="width:{{ pct }}%"></div></div></td>
        </tr>
        {% else %}<tr><td class="empty">Пока пусто</td></tr>{% endfor %}
      </table>
    </div>
  </div>
</div>

<footer>Proqnozai Bot Dashboard · данные обновляются автоматически каждые 45 секунд</footer>

<script>
let dailyLabels = {{ daily_labels|tojson }};
let dailyData   = {{ daily_values|tojson }};
let fcLabels    = {{ fc_labels|tojson }};
let fcData      = {{ fc_values|tojson }};
let langLabels  = {{ lang_labels|tojson }};
let langData    = {{ lang_values|tojson }};
let fbData      = {{ fb_data|tojson }};
let winrateLabels = {{ winrate_labels|tojson }};
let winrateData   = {{ winrate_values|tojson }};

const COLORS = ['#6c63ff','#38bdf8','#22c55e','#f59e0b','#ef4444','#a78bfa','#fb923c'];
let lineChart, langChart, barChart, feedbackChart, winrateChart;

function cssVar(n){ return getComputedStyle(document.body).getPropertyValue(n).trim(); }

// Chart.js comes from a CDN. If that host is unreachable the global is
// undefined, and touching Chart.* used to throw — taking auto-refresh, the
// theme toggle and everything else on the page down with it. The numbers and
// tables don't need the library, so a missing Chart costs only the graphs.
function chartsAvailable(){ return typeof Chart !== 'undefined'; }
function noteChartsUnavailable(){
  document.querySelectorAll('canvas').forEach(cv=>{
    const note=document.createElement('div');
    note.className='chart-fallback';
    note.textContent='Графики недоступны: не загрузилась библиотека Chart.js.';
    if(cv.parentNode) cv.parentNode.replaceChild(note,cv);
  });
}

function axes(maxY){
  return {
    x:{grid:{display:false},ticks:{color:cssVar('--muted'),maxTicksLimit:7}},
    y:{grid:{color:cssVar('--border')},ticks:{color:cssVar('--muted'),precision:0},
       beginAtZero:true, max:maxY}
  };
}
const noLegend={legend:{display:false}};
const donutLegend={legend:{position:'bottom',labels:{color:cssVar('--muted'),padding:10,font:{size:11},boxWidth:10,usePointStyle:true}}};

function makeCharts(){
  if(!chartsAvailable()){ noteChartsUnavailable(); return; }
  Chart.defaults.color = cssVar('--muted');
  Chart.defaults.borderColor = cssVar('--border');
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

  lineChart = new Chart(document.getElementById('lineChart'), {
    type:'line',
    data:{labels:dailyLabels,datasets:[{label:'Запросы',data:dailyData,
      borderColor:cssVar('--accent'),backgroundColor:cssVar('--accent')+'22',
      borderWidth:2,pointRadius:2,pointHoverRadius:5,fill:true,tension:.35}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:noLegend,scales:axes()}
  });

  langChart = new Chart(document.getElementById('langChart'), {
    type:'doughnut',
    data:{labels:langLabels,datasets:[{data:langData,backgroundColor:COLORS,
      borderWidth:2,borderColor:cssVar('--surface')}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:donutLegend,cutout:'68%'}
  });

  // Forecast volume — its own series, not a second view of the request count.
  barChart = new Chart(document.getElementById('barChart'), {
    type:'bar',
    data:{labels:fcLabels,datasets:[{label:'Прогнозы',data:fcData,
      backgroundColor:cssVar('--accent')+'cc',borderRadius:6,maxBarThickness:26}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:noLegend,scales:axes()}
  });

  feedbackChart = new Chart(document.getElementById('feedbackChart'), {
    type:'doughnut',
    data:{labels:['Победы','Проигрыши','Без оценки'],datasets:[{data:fbData,
      backgroundColor:[cssVar('--ok'),cssVar('--bad'),cssVar('--border')],
      borderWidth:2,borderColor:cssVar('--surface')}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:donutLegend,cutout:'68%'}
  });

  winrateChart = new Chart(document.getElementById('winrateChart'), {
    type:'line',
    data:{labels:winrateLabels,datasets:[{label:'Win-rate %',data:winrateData,
      borderColor:cssVar('--ok'),backgroundColor:cssVar('--ok')+'22',
      borderWidth:2,pointRadius:2,fill:true,tension:.35}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:noLegend,scales:axes(100)}
  });
}

function updateCharts(){
  if(!chartsAvailable()) return;
  [lineChart,langChart,barChart,feedbackChart,winrateChart].forEach(c=>{ if(c) c.destroy(); });
  setTimeout(makeCharts, 40);
}
makeCharts();

// ── AJAX refresh: keeps theme, scroll and the open page state ────────────────
function setText(id,v){ const el=document.getElementById(id); if(el) el.textContent=v; }
async function refreshData(){
  try{
    const r = await fetch('/api/data'); if(!r.ok) return;
    const x = await r.json();
    setText('k_users', x.users_total);
    setText('k_new', '+' + (x.users_today||0));
    setText('k_dau', x.users_active_today);
    setText('k_forecasts', x.forecasts_real_total ?? x.forecasts_total);
    setText('k_reqs', x.reqs_total);
    const fbt=x.fb_total||0, fbw=x.fb_wins||0;
    setText('k_acc', (fbt ? Math.round(fbw/fbt*100) : 0) + '%');
    dailyLabels=(x.daily||[]).map(r=>r[0].slice(5));
    dailyData=(x.daily||[]).map(r=>r[1]);
    fcLabels=(x.forecasts_daily||[]).map(r=>r[0].slice(5));
    fcData=(x.forecasts_daily||[]).map(r=>r[1]);
    langLabels=(x.langs||[]).map(r=>r[0]);
    langData=(x.langs||[]).map(r=>r[1]);
    fbData=[fbw, fbt-fbw, Math.max(0,(x.forecasts_real_total||0)-fbt)];
    winrateLabels=(x.winrate_daily||[]).map(r=>r[0].slice(5));
    winrateData=(x.winrate_daily||[]).map(r=> r[2] ? Math.round(r[1]/r[2]*100) : 0);
    updateCharts();
    setText('stamp', '🔄 ' + new Date().toLocaleTimeString('ru-RU'));
  }catch(e){}
}
setInterval(refreshData, 45000);
</script>
"""

TEMPLATE = _page(_STATS_BODY, "Proqnozai — Дашборд").replace(
    "{% block head %}{% endblock %}",
    '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>')


# A delta chip, rendered from the dict _delta() produces. A macro rather than
# five copies of the same conditional markup.
_CHIP = r"""
{% macro chip(d) %}{% if d and d.show %}<span class="delta {{ d.cls }}">{{ d.arrow }}{{ d.pct }}%</span>{% endif %}{% endmacro %}
"""
TEMPLATE = _CHIP + TEMPLATE


# ─── Routes ───────────────────────────────────────────────────────────────────
# Numeric keys the template compares or divides. A backend that is older than
# the dashboard (the two deploy independently) simply omits the newest ones, and
# an Undefined in an arithmetic comparison raises mid-render — which the auth
# wrapper then reports as a 401. Defaults keep a partial payload renderable.
_NUM_DEFAULTS = (
    "users_total", "users_today", "users_week", "users_prev_week", "users_blocked",
    "users_active_today", "users_active_yday", "users_active_week",
    "reqs_total", "reqs_today", "reqs_week", "reqs_prev_week",
    "forecasts_total", "forecasts_today", "forecasts_real_total", "forecasts_real_today",
    "forecasts_week", "forecasts_prev_week", "repeat_users", "repeat_pct",
    "users_with_activity", "live_subs", "live_matches", "fb_total", "fb_wins",
)


def _delta(cur: int, prev: int) -> dict:
    """Week-over-week change as a renderable chip.

    Hidden when there is no baseline: "+100%" against a first week of zero says
    nothing, and a chip that is always green stops being read."""
    if not prev:
        return {"show": False}
    pct = round((cur - prev) / prev * 100)
    return {"show": True, "pct": abs(pct),
            "cls": "d-up" if pct > 0 else "d-down" if pct < 0 else "d-flat",
            "arrow": "↑" if pct > 0 else "↓" if pct < 0 else "→"}


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

    for key in _NUM_DEFAULTS:
        raw.setdefault(key, 0)
    for key in ("langs", "top_users", "recent_users", "recent_forecasts",
                "daily", "forecasts_daily", "winrate_daily", "by_action", "retention"):
        raw.setdefault(key, [])

    fb_total = raw.get("fb_total", 0)
    fb_wins  = raw.get("fb_wins", 0)
    fb_lose  = fb_total - fb_wins
    raw["fb_pct"] = round(fb_wins / fb_total * 100) if fb_total else 0

    daily        = raw.get("daily", [])
    daily_labels = [r[0][5:] for r in daily]
    daily_values = [r[1] for r in daily]

    fc_daily  = raw.get("forecasts_daily", [])
    fc_labels = [r[0][5:] for r in fc_daily]
    fc_values = [r[1] for r in fc_daily]

    langs       = raw.get("langs", [])
    lang_labels = [r[0] for r in langs]
    lang_values = [r[1] for r in langs]

    # Unrated = forecasts actually produced minus the ones anyone rated. The
    # event log is the denominator; forecast_history keeps only 10 rows per user.
    fb_unrated = max(0, raw.get("forecasts_real_total", 0) - fb_total)

    wr = raw.get("winrate_daily", [])
    winrate_labels = [r[0][5:] for r in wr]
    winrate_values = [round(r[1] / r[2] * 100) if r[2] else 0 for r in wr]

    deltas = {
        "users":     _delta(raw.get("users_week", 0), raw.get("users_prev_week", 0)),
        "dau":       _delta(raw.get("users_active_today", 0), raw.get("users_active_yday", 0)),
        "reqs":      _delta(raw.get("reqs_week", 0), raw.get("reqs_prev_week", 0)),
        "forecasts": _delta(raw.get("forecasts_week", 0), raw.get("forecasts_prev_week", 0)),
    }

    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")

    class D:
        pass
    d = D()
    d.__dict__.update(raw)

    return render_template_string(
        TEMPLATE, d=d, dl=deltas, bc=raw.get("broadcasts", {}),
        daily_labels=daily_labels, daily_values=daily_values,
        fc_labels=fc_labels, fc_values=fc_values,
        lang_labels=lang_labels, lang_values=lang_values,
        fb_data=[fb_wins, fb_lose, fb_unrated],
        winrate_labels=winrate_labels, winrate_values=winrate_values,
        page="stats", subtitle="Онлайн · обновление каждые 45с", stamp=stamp,
    )


def _auth_headers() -> dict:
    """Token goes in a header, never in the URL: a query string lands in proxy
    access logs and browser history. The worker still accepts ?token= so the two
    services can be redeployed in either order."""
    return {"X-Dashboard-Token": STATS_TOKEN} if STATS_TOKEN else {}


def _proxy_get(path: str, params: dict | None = None, timeout: int = 8) -> Response:
    """Pass a worker GET through to the browser. Every /api/* read does the same
    three things — call, forward, degrade — so they say it once."""
    try:
        resp = httpx.get(f"{_BOT_BASE}{path}", params=params,
                         headers=_auth_headers(), timeout=timeout)
        return Response(resp.text, mimetype="application/json", status=resp.status_code)
    except Exception as e:
        logger.warning("stats backend unavailable for %s: %s", path, _safe_err(e))
        return _backend_error_json()


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
    return _proxy_get("/broadcast/status")


@app.route("/api/broadcast/list")
@require_auth
def api_broadcast_list():
    """The worker stores UTC; the operator reads Moscow time. Converting here
    keeps one timezone rule in the web process instead of two in JavaScript."""
    try:
        resp = httpx.get(f"{_BOT_BASE}/broadcast/list", headers=_auth_headers(), timeout=8)
        if resp.status_code != 200:
            return Response(resp.text, mimetype="application/json", status=resp.status_code)
        items = (resp.json() or {}).get("items", [])
        for it in items:
            it["run_at_local"] = _msk(it.get("run_at", ""))
            it["created_at_local"] = _msk(it.get("created_at", ""))
        return Response(json.dumps({"items": items}, ensure_ascii=False),
                        mimetype="application/json")
    except Exception as e:
        logger.warning("stats backend unavailable for API route: %s", _safe_err(e))
        return _backend_error_json()


@app.route("/api/segment/size")
@require_auth
def api_segment_size():
    return _proxy_get("/segment/size", params={"s": request.args.get("s", "all")})


@app.route("/api/broadcast/cancel", methods=["POST"])
@require_auth
def api_broadcast_cancel():
    if not csrf_ok():
        logger.warning("broadcast cancel rejected: CSRF check failed")
        return Response("CSRF check failed", 403)
    body = request.get_json(silent=True) or {}
    try:
        resp = httpx.post(f"{_BOT_BASE}/broadcast/cancel", headers=_auth_headers(),
                          json={"id": body.get("id")}, timeout=8)
        return Response(resp.text, mimetype="application/json", status=resp.status_code)
    except Exception as e:
        logger.warning("stats backend unavailable for API route: %s", _safe_err(e))
        return _backend_error_json()


@app.route("/api/users/search")
@require_auth
def api_users_search():
    return _proxy_get("/users/search", params={"q": request.args.get("q", "").strip()})


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



# ─── Users page ───────────────────────────────────────────────────────────────
_USERS_BODY = r"""
<div class="container" style="max-width:1020px">
  <div class="card">
    <h3>🔍 Поиск пользователя</h3>
    <div class="row">
      <input id="q" placeholder="ID, @username или имя..." onkeydown="if(event.key==='Enter')doSearch()">
      <button class="btn primary" onclick="doSearch()">Найти</button>
    </div>
  </div>
  <div id="result" style="margin-top:14px"><div class="empty">Введите запрос для поиска пользователей.</div></div>
</div>
<script>
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function doSearch(){
  const q = document.getElementById('q').value.trim();
  const box = document.getElementById('result');
  if(!q){ box.innerHTML = '<div class="empty">Введите запрос.</div>'; return; }
  box.innerHTML = '<div class="empty">Поиск…</div>';
  try{
    const r = await fetch('/api/users/search?q=' + encodeURIComponent(q));
    const data = await r.json();
    const users = data.users || [];
    if(!users.length){ box.innerHTML = '<div class="empty">Ничего не найдено.</div>'; return; }
    let html = '<div class="card tbl-wrap"><table><tr><th>ID</th><th>Имя</th><th>Username</th><th>Язык</th><th>Запросов</th><th>Статус</th><th></th></tr>';
    for(const u of users){
      const b = u.is_blocked;
      html += '<tr>'
        + '<td class="muted">'+esc(u.user_id)+'</td>'
        + '<td><strong>'+esc(u.display_name||'—')+'</strong></td>'
        + '<td class="muted">@'+esc(u.username||'-')+'</td>'
        + '<td><span class="badge lang">'+esc(u.lang||'')+'</span></td>'
        + '<td>'+esc(u.total_requests||0)+'</td>'
        + '<td>'+(b?'<span class="badge lose">🚫 Заблокирован</span>':'<span class="badge win">✅ Активен</span>')+'</td>'
        + '<td><button class="btn sm '+(b?'':'danger')+'" onclick="toggleBlock('+Number(u.user_id)+','+(b?0:1)+')">'
        + (b?'Разблокировать':'Заблокировать')+'</button></td></tr>';
    }
    box.innerHTML = html + '</table></div>';
  }catch(e){ box.innerHTML = '<div class="empty">Ошибка загрузки.</div>'; }
}
async function toggleBlock(uid, blocked){
  try{
    await fetch('/api/users/block', {method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':'{{ csrf }}'},
      body: JSON.stringify({user_id: uid, blocked: blocked})});
    doSearch();
  }catch(e){ alert('Не удалось изменить статус'); }
}
</script>
"""

USERS_TEMPLATE = _page(_USERS_BODY, "Proqnozai — Пользователи").replace(
    "{% block head %}{% endblock %}", "")


@app.route("/users")
@require_auth
def users_page():
    return render_template_string(USERS_TEMPLATE, csrf=csrf_token(),
                                  page="users", subtitle="Управление доступом",
                                  stamp="")



# ─── Partners page ────────────────────────────────────────────────────────────
# The markup and script are the partner manager as shipped; only the shell (CSS,
# header, nav) is the shared one, so this page stops looking like a different
# product from the rest of the dashboard.
_PARTNERS_EXTRA_CSS = r"""
/* ── Partners page ── */
.toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px;flex-wrap:wrap;}
.card.off{opacity:.6;}
.row1{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;}
.pname{font-size:16px;font-weight:700;}
.b-on{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok);}
.b-off{background:var(--surface2);color:var(--muted);}
.b-warn{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn);}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-top:14px;}
.f-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;}
.f-val{font-size:13px;word-break:break-all;}
.actions{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap;}
.pool-note{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin-top:12px;font-size:12px;color:var(--muted);}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
@media(max-width:560px){.grid2{grid-template-columns:1fr;}}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:16px;z-index:200;}
.modal-bg.open{display:flex;}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:22px;
  max-width:540px;width:100%;max-height:90vh;overflow:auto;box-shadow:var(--shadow);}
.modal h2{font-size:16px;margin-bottom:6px;}
.msg{margin-top:14px;padding:10px 12px;border-radius:10px;font-size:13px;display:none;}
.msg.err{display:block;background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad);}
.msg.ok{display:block;background:color-mix(in srgb,var(--ok) 16%,transparent);color:var(--ok);}
.hint{color:var(--muted);text-align:center;padding:34px;}
/* Their markup predates the shared button classes; keep the old names working
   rather than rewriting several hundred lines of generated HTML. */
.btn-primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600;}
.btn-primary:hover{background:var(--accent2);border-color:var(--accent2);color:#fff;}
.btn-danger{border-color:var(--bad);color:var(--bad);}
.card{margin-bottom:14px;}
label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin:12px 0 5px;}
.chk{display:flex;align-items:center;gap:8px;margin-top:14px;}
.chk span{font-size:13px;color:var(--text);}
"""

_PARTNERS_BODY = r"""
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
"""

PARTNERS_TEMPLATE = _page(_PARTNERS_BODY, "Proqnozai — Партнёры").replace(
    "{% block head %}{% endblock %}", "<style>" + _PARTNERS_EXTRA_CSS + "</style>")


@app.route("/partners")
@require_auth
def partners_page():
    return render_template_string(PARTNERS_TEMPLATE, csrf=csrf_token(),
                                  page="partners", subtitle="Партнёры и промокоды",
                                  stamp="")


# ─── Broadcast page ───────────────────────────────────────────────────────────
_BROADCAST_BODY = r"""
<div class="container" style="max-width:1180px">

  {% if result %}
  <div class="card" style="margin-bottom:14px;border-color:{{ 'var(--ok)' if result.ok else 'var(--bad)' }}">
    {% if result.ok %}
      {% if result.scheduled %}🗓 Рассылка запланирована на <b>{{ result.when }}</b> (МСК) для
        <b>{{ result.recipients }}</b> чел. Её можно отменить в списке ниже до момента отправки.
      {% else %}🚀 Рассылка запущена для <b>{{ result.recipients }}</b> чел. Прогресс — ниже.{% endif %}
    {% else %}❌ {{ result.error }}{% endif %}
  </div>
  {% endif %}

  <div class="grid g2" style="align-items:start">
    <!-- ── Composer ── -->
    <div class="card">
      <h3>📢 Новая рассылка</h3>
      <form id="bform" method="POST" action="/broadcast">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <input type="hidden" name="buttons" id="buttonsJson" value="">

        <div class="field">
          <label class="f" for="segment">Аудитория <span id="segSize" class="badge"></span></label>
          <select name="segment" id="segment" onchange="segChanged()">
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
            <optgroup label="По виду спорта">
              <option value="sport:football">⚽ Футбол</option>
              <option value="sport:ufc">🥊 UFC/MMA</option>
              <option value="sport:nba">🏀 Баскетбол</option>
              <option value="sport:tennis">🎾 Теннис</option>
              <option value="sport:hockey">🏒 Хоккей</option>
              <option value="sport:all">🏆 Все виды</option>
            </optgroup>
            <optgroup label="По активности">
              <option value="act:active">🟢 Активные (≤7 дней)</option>
              <option value="act:churn">🟡 Отток (7–30 дней)</option>
              <option value="act:sleep">🔴 Спящие (&gt;30 дней)</option>
              <option value="act:never">⚪ Ни одного действия</option>
            </optgroup>
          </select>
        </div>

        <div class="field">
          <label class="f">Текст сообщения (HTML)</label>
          <div class="row" style="gap:6px;margin-bottom:8px;flex-wrap:wrap">
            <button type="button" class="btn sm" onclick="wrap('b')"><b>Ж</b></button>
            <button type="button" class="btn sm" onclick="wrap('i')"><i>К</i></button>
            <button type="button" class="btn sm" onclick="wrap('u')"><u>Ч</u></button>
            <button type="button" class="btn sm" onclick="wrap('s')"><s>З</s></button>
            <button type="button" class="btn sm" onclick="wrap('code')">&lt;/&gt;</button>
            <button type="button" class="btn sm" onclick="insertLink()">🔗 Ссылка</button>
            <button type="button" class="btn sm" onclick="insertTag('tg-spoiler')">🙈 Спойлер</button>
          </div>
          <textarea name="text" id="text" maxlength="4096" oninput="renderPreview()"
                    placeholder="Привет! Сегодня разбираем <b>топ-матч</b> дня — <a href=&quot;https://t.me/…&quot;>смотреть</a>">{{ prefill or '' }}</textarea>
          <div class="sub" style="text-align:right;margin-top:4px">
            <span id="charCount">0</span> / 4096 · поддерживаются &lt;b&gt; &lt;i&gt; &lt;u&gt; &lt;s&gt; &lt;code&gt; &lt;a href&gt;
          </div>
        </div>

        <div class="field">
          <label class="f">Кнопки под сообщением</label>
          <div id="buttons"></div>
          <button type="button" class="btn sm" onclick="addButton()">＋ Добавить кнопку</button>
          <div class="sub" style="margin-top:6px">До 8 кнопок, ссылка вида https://… или tg://…</div>
        </div>

        <div class="field">
          <label class="f">Время отправки (МСК)</label>
          <div class="row" style="flex-wrap:wrap">
            <input type="datetime-local" name="run_at" id="runAt" style="flex:1;min-width:190px" onchange="renderSubmit()">
            <button type="button" class="btn sm" onclick="preset(1)">+1 ч</button>
            <button type="button" class="btn sm" onclick="preset(3)">+3 ч</button>
            <button type="button" class="btn sm" onclick="presetTomorrow()">Завтра 10:00</button>
            <button type="button" class="btn sm" onclick="clearTime()">Сейчас</button>
          </div>
        </div>

        <div class="field">
          <label class="chk"><input type="checkbox" name="no_preview" id="noPreview" onchange="renderPreview()"> Не показывать превью ссылок</label>
        </div>

        <hr style="border:none;border-top:1px solid var(--border);margin:18px 0">
        <button type="submit" class="btn primary" id="submitBtn">📤 Отправить сейчас</button>
        <span class="sub" style="margin-left:10px">Действие необратимо</span>
      </form>
    </div>

    <!-- ── Preview + progress + queue ── -->
    <div>
      <div class="card">
        <h3>👀 Превью</h3>
        <div style="background:var(--surface2);border:1px solid var(--border);border-radius:14px;
                    padding:14px 16px;min-height:90px">
          <div id="preview" style="font-size:14px;white-space:pre-wrap;word-break:break-word">
            <span class="muted">Начните вводить текст…</span>
          </div>
          <div id="previewButtons" style="margin-top:10px;display:flex;flex-direction:column;gap:6px"></div>
        </div>
        <div id="previewError" class="sub bad" style="margin-top:8px"></div>
      </div>

      <div class="card" id="progressCard" style="margin-top:14px;display:none">
        <h3>⏳ Прогресс отправки</h3>
        <div class="bar-wrap" style="height:10px"><div class="bar" id="bcastBar" style="height:10px;width:0%"></div></div>
        <div class="sub" id="bcastText" style="margin-top:8px"></div>
      </div>

      <div class="card" style="margin-top:14px">
        <h3>🗓 Очередь и история</h3>
        <div id="queue"><div class="empty">Загрузка…</div></div>
      </div>
    </div>
  </div>
</div>

<script>
const CSRF = '{{ csrf }}';
const ta = document.getElementById('text');

// ── Formatting helpers ──────────────────────────────────────────────────────
function insertTag(tag, attrs){
  const s = ta.selectionStart, e = ta.selectionEnd, v = ta.value;
  const open = '<' + tag + (attrs ? ' ' + attrs : '') + '>';
  ta.value = v.slice(0,s) + open + v.slice(s,e) + '</' + tag + '>' + v.slice(e);
  ta.focus();
  ta.selectionStart = s + open.length; ta.selectionEnd = e + open.length;
  renderPreview();
}
function wrap(tag){ insertTag(tag); }
function insertLink(){
  const url = prompt('Ссылка (https://… или tg://…)');
  if(!url) return;
  if(!/^(https?:\/\/|tg:\/\/)/.test(url)){ alert('Ссылка должна начинаться с http://, https:// или tg://'); return; }
  insertTag('a', 'href="' + url.replace(/"/g,'&quot;') + '"');
}

// ── Buttons builder ─────────────────────────────────────────────────────────
function addButton(text, url){
  const box = document.getElementById('buttons');
  if(box.children.length >= 8){ alert('Максимум 8 кнопок'); return; }
  const row = document.createElement('div');
  row.className = 'row';
  row.style.marginBottom = '8px';
  row.innerHTML =
    '<input placeholder="Текст кнопки" maxlength="64" style="flex:1">' +
    '<input placeholder="https://…" style="flex:2">' +
    '<button type="button" class="btn sm danger">✕</button>';
  row.querySelectorAll('input').forEach(i=>i.addEventListener('input', renderPreview));
  row.querySelector('button').onclick = ()=>{ row.remove(); renderPreview(); };
  if(text) row.children[0].value = text;
  if(url)  row.children[1].value = url;
  box.appendChild(row);
  renderPreview();
}
function collectButtons(){
  // One button per row: Telegram allows several side by side, but a broadcast
  // CTA reads better full width and it keeps the builder to one input pair.
  return [...document.getElementById('buttons').children]
    .map(r => ({text: r.children[0].value.trim(), url: r.children[1].value.trim()}))
    .filter(b => b.text && b.url)
    .map(b => [b]);
}

// ── Preview ─────────────────────────────────────────────────────────────────
const ALLOWED = ['b','strong','i','em','u','ins','s','strike','del','a','code','pre','blockquote','span','tg-spoiler'];
function previewHtml(src){
  // Render only Telegram's own subset; anything else is shown as literal text,
  // which is also how Telegram would reject it.
  const escaped = src.replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  return escaped.replace(
    /&lt;(\/?)([a-z-]+)((?:\s+href="[^"]*")?)\s*&gt;/gi,
    (m, slash, tag, attr) => {
      if(!ALLOWED.includes(tag.toLowerCase())) return m;
      if(tag.toLowerCase()==='a' && !slash){
        const href = (attr.match(/href="([^"]*)"/i)||[])[1] || '';
        if(!/^(https?:\/\/|tg:\/\/)/.test(href)) return m;
        return '<a href="#" onclick="return false" style="color:var(--info)">';
      }
      return '<' + slash + tag + '>';
    });
}
function renderPreview(){
  const src = ta.value;
  document.getElementById('charCount').textContent = src.length;
  const box = document.getElementById('preview');
  box.innerHTML = src ? previewHtml(src) : '<span class="muted">Начните вводить текст…</span>';

  const pb = document.getElementById('previewButtons');
  pb.innerHTML = '';
  for(const row of collectButtons()){
    const b = document.createElement('div');
    b.textContent = row[0].text;
    b.style.cssText = 'background:var(--surface);border:1px solid var(--border);border-radius:9px;'
      + 'padding:8px;text-align:center;font-size:13px;font-weight:600;color:var(--info)';
    pb.appendChild(b);
  }
  document.getElementById('previewError').textContent = checkHtml(src) || '';
  renderSubmit();
}
function checkHtml(src){
  // Cheap balance check so a broken tag is visible before the send, not after
  // Telegram rejects the whole campaign. The worker validates again.
  const stack = [];
  const re = /<(\/?)([a-z-]+)(\s[^>]*)?>/gi;
  let m;
  while((m = re.exec(src))){
    const tag = m[2].toLowerCase();
    if(!ALLOWED.includes(tag)) return 'Тег <' + tag + '> не поддерживается Telegram';
    if(m[1]){ if(stack.pop() !== tag) return 'Лишний или неверный закрывающий тег </' + tag + '>'; }
    else stack.push(tag);
  }
  return stack.length ? 'Не закрыт тег <' + stack[stack.length-1] + '>' : '';
}

// ── Scheduling ──────────────────────────────────────────────────────────────
function localValue(d){
  const p = n => String(n).padStart(2,'0');
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+'T'+p(d.getHours())+':'+p(d.getMinutes());
}
// The input is Moscow time whatever the operator's own clock says, so presets
// are computed in Moscow rather than from the browser's timezone.
function mskNow(){
  const now = new Date();
  return new Date(now.getTime() + (now.getTimezoneOffset() + 180) * 60000);
}
function preset(h){ const d = mskNow(); d.setHours(d.getHours()+h); document.getElementById('runAt').value = localValue(d); renderSubmit(); }
function presetTomorrow(){ const d = mskNow(); d.setDate(d.getDate()+1); d.setHours(10,0,0,0); document.getElementById('runAt').value = localValue(d); renderSubmit(); }
function clearTime(){ document.getElementById('runAt').value=''; renderSubmit(); }
function renderSubmit(){
  const when = document.getElementById('runAt').value;
  document.getElementById('submitBtn').textContent = when ? '🗓 Запланировать' : '📤 Отправить сейчас';
}

// ── Segment size ────────────────────────────────────────────────────────────
async function segChanged(){
  const seg = document.getElementById('segment').value;
  const badge = document.getElementById('segSize');
  badge.textContent = '…';
  try{
    const r = await fetch('/api/segment/size?s=' + encodeURIComponent(seg));
    const d = await r.json();
    badge.textContent = (d.size ?? '?') + ' получателей';
  }catch(e){ badge.textContent = ''; }
}

// ── Submit ──────────────────────────────────────────────────────────────────
document.getElementById('bform').addEventListener('submit', function(ev){
  const err = checkHtml(ta.value);
  if(!ta.value.trim()){ ev.preventDefault(); alert('Введите текст'); return; }
  if(err){ ev.preventDefault(); alert(err); return; }
  document.getElementById('buttonsJson').value = JSON.stringify(collectButtons());
  const seg = document.getElementById('segment');
  const when = document.getElementById('runAt').value;
  const what = when ? 'Запланировать рассылку на ' + when.replace('T',' ') + ' (МСК)?'
                    : 'Отправить рассылку прямо сейчас?';
  if(!confirm(what + '\nАудитория: ' + seg.options[seg.selectedIndex].text)) ev.preventDefault();
});

// ── Progress + queue ────────────────────────────────────────────────────────
async function pollBroadcast(){
  try{
    const r = await fetch('/api/broadcast/status'); if(!r.ok) return;
    const s = await r.json();
    const total = s.total||0, done = (s.ok||0)+(s.fail||0);
    if(s.running || (s.done && total)){
      document.getElementById('progressCard').style.display = 'block';
      const pct = total ? Math.round(done/total*100) : 0;
      document.getElementById('bcastBar').style.width = pct + '%';
      document.getElementById('bcastText').textContent =
        (s.running ? '⏳ Идёт рассылка' : '✅ Завершено') +
        `: ${done}/${total} · доставлено ${s.ok||0} · ошибок ${s.fail||0}`;
    }
  }catch(e){}
}
const STATUS = {pending:['Запланировано','badge'],running:['Отправляется','badge lang'],
                done:['Отправлено','badge win'],failed:['Сбой','badge lose'],
                canceled:['Отменено','badge']};
async function loadQueue(){
  const box = document.getElementById('queue');
  try{
    const r = await fetch('/api/broadcast/list');
    const items = (await r.json()).items || [];
    if(!items.length){ box.innerHTML = '<div class="empty">Рассылок пока не было.</div>'; return; }
    let html = '<div class="tbl-wrap"><table>';
    for(const b of items){
      const [label, cls] = STATUS[b.status] || [b.status,'badge'];
      const when = b.run_at_local || b.created_at_local || '';
      const text = (b.text||'').replace(/<[^>]+>/g,'').slice(0,46);
      html += '<tr><td><span class="'+cls+'">'+label+'</span></td>'
        + '<td><div style="font-size:13px">'+esc(text)+'…</div>'
        + '<div class="sub">'+esc(when)+' · '+esc(b.segment)+'</div></td>'
        + '<td class="sub">'+(b.status==='pending' ? '' : (b.ok||0)+'/'+(b.total||0))+'</td>'
        + '<td>'+(b.status==='pending'
            ? '<button class="btn sm danger" onclick="cancelBcast('+Number(b.id)+')">Отменить</button>' : '')+'</td></tr>';
    }
    box.innerHTML = html + '</table></div>';
  }catch(e){ box.innerHTML = '<div class="empty">Не удалось загрузить список.</div>'; }
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function cancelBcast(id){
  if(!confirm('Отменить запланированную рассылку?')) return;
  try{
    await fetch('/api/broadcast/cancel', {method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},
      body: JSON.stringify({id})});
    loadQueue();
  }catch(e){ alert('Не удалось отменить'); }
}

renderPreview(); segChanged(); loadQueue(); pollBroadcast();
setInterval(pollBroadcast, 2000);
setInterval(loadQueue, 15000);
</script>
"""

BROADCAST_TEMPLATE = _page(_BROADCAST_BODY, "Proqnozai — Рассылка").replace(
    "{% block head %}{% endblock %}", "")


def _broadcast_result(resp_status: int, data: dict) -> dict:
    """Worker response → what the page shows the operator."""
    if resp_status in (200, 202):
        return {"ok": True, "scheduled": bool(data.get("scheduled")),
                "recipients": data.get("recipients", data.get("started", 0)),
                "when": _msk(data.get("run_at", ""))}
    if resp_status == 409:
        return {"ok": False, "error": "Рассылка уже выполняется, дождитесь её окончания."}
    return {"ok": False, "error": data.get("error") or f"Ошибка воркера (HTTP {resp_status})"}


def _msk(utc_str: str) -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' from the worker → Moscow time for the operator."""
    if not utc_str:
        return ""
    from datetime import datetime, timedelta, timezone
    try:
        dt = datetime.strptime(utc_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return utc_str
    return dt.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m %H:%M")


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
        run_at  = (request.form.get("run_at") or "").strip()
        prefill = text
        try:
            buttons = json.loads(request.form.get("buttons") or "[]")
        except ValueError:
            buttons = []

        if not text:
            result = {"ok": False, "error": "Пустой текст"}
        else:
            try:
                resp = httpx.post(
                    BROADCAST_URL, headers=_auth_headers(),
                    json={"text": text, "segment": segment, "buttons": buttons,
                          "run_at": run_at,
                          "no_preview": bool(request.form.get("no_preview"))},
                    timeout=15,
                )
                data = resp.json()
                result = _broadcast_result(resp.status_code, data if isinstance(data, dict) else {})
                if result["ok"]:
                    prefill = ""   # accepted: don't re-offer the same message
            except Exception as e:
                logger.warning("broadcast backend unavailable: %s", _safe_err(e))
                result = {"ok": False, "error": "Сервис недоступен, попробуйте позже."}

    return render_template_string(BROADCAST_TEMPLATE, result=result, prefill=prefill,
                                  csrf=csrf_token(), page="broadcast",
                                  subtitle="Сообщения пользователям бота", stamp="")


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
