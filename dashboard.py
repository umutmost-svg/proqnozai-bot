"""
Proqnozai Bot Dashboard — improved version.
Auth: HTTP Basic Auth (login: admin, password: DASHBOARD_TOKEN)
Stats source: bot's internal stats server (stats_server.py via Railway private network)
"""
import base64
import json
import os
from functools import wraps

import httpx
from flask import Flask, Response, render_template_string, request, redirect, url_for

app = Flask(__name__)

_BOT_BASE     = os.environ.get("BOT_API_URL", "http://worker.railway.internal:8888")
STATS_URL     = os.environ.get("STATS_URL", _BOT_BASE + "/stats")
BROADCAST_URL = _BOT_BASE + "/broadcast"
STATS_TOKEN   = os.environ.get("DASHBOARD_TOKEN", "")
DASH_USER     = os.environ.get("DASHBOARD_USER", "admin")


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
                if user == DASH_USER and pwd == STATS_TOKEN:
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
      <div class="stat-value accent" id="k_forecasts">{{ d.forecasts_total }}</div>
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

function makeCharts() {
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
    setText('k_forecasts', x.forecasts_total);
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
    token_param = f"?token={STATS_TOKEN}" if STATS_TOKEN else ""
    try:
        resp = httpx.get(STATS_URL + token_param, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return f"<pre style='color:red;padding:20px'>Ошибка получения данных:\n{e}\n\nURL: {STATS_URL}</pre>", 500

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

    from datetime import datetime
    generated_at = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")

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


def _stats_param():
    return f"?token={STATS_TOKEN}" if STATS_TOKEN else ""


@app.route("/api/data")
@require_auth
def api_data():
    """JSON stats for the dashboard's AJAX auto-refresh."""
    try:
        resp = httpx.get(STATS_URL + _stats_param(), timeout=10)
        resp.raise_for_status()
        return Response(resp.text, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=502, mimetype="application/json")


@app.route("/api/broadcast/status")
@require_auth
def api_broadcast_status():
    try:
        resp = httpx.get(f"{_BOT_BASE}/broadcast/status" + _stats_param(), timeout=8)
        return Response(resp.text, mimetype="application/json", status=resp.status_code)
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=502, mimetype="application/json")


@app.route("/api/users/search")
@require_auth
def api_users_search():
    q = request.args.get("q", "").strip()
    try:
        resp = httpx.get(f"{_BOT_BASE}/users/search",
                         params={"token": STATS_TOKEN, "q": q}, timeout=8)
        return Response(resp.text, mimetype="application/json", status=resp.status_code)
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=502, mimetype="application/json")


@app.route("/api/users/block", methods=["POST"])
@require_auth
def api_users_block():
    body = request.get_json(silent=True) or {}
    try:
        resp = httpx.post(f"{_BOT_BASE}/users/block", json={
            "token": STATS_TOKEN,
            "user_id": body.get("user_id"),
            "blocked": body.get("blocked"),
        }, timeout=8)
        return Response(resp.text, mimetype="application/json", status=resp.status_code)
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=502, mimetype="application/json")


@app.route("/users")
@require_auth
def users_page():
    return render_template_string(USERS_TEMPLATE)


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
    await fetch('/api/users/block', {method:'POST', headers:{'Content-Type':'application/json'},
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
        text    = (request.form.get("text") or "").strip()
        segment = request.form.get("segment", "all")
        prefill = text

        if not text:
            result = {"started": 0, "error": "Пустой текст"}
        else:
            try:
                resp = httpx.post(
                    BROADCAST_URL,
                    json={"token": STATS_TOKEN, "text": text, "segment": segment},
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
                result = {"started": 0, "error": str(e)}

    return render_template_string(BROADCAST_TEMPLATE, result=result, prefill=prefill)


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
