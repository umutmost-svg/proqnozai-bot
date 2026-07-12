# Dashboard Deployment

How to run and deploy the ProqnozAI web dashboard. All values here are derived
from the code (`Procfile`, `dashboard.py`, `stats_server.py`), not guessed.

## Architecture (as built)

- **Frontend:** server-rendered HTML via Flask `render_template_string` (inline
  templates in `dashboard.py`); Chart.js is loaded from a CDN. No local
  `static/`/`templates/` directory.
- **Backend:** Flask app (`dashboard.py`).
- **Entrypoint:** `dashboard.py` → `app.run(host="0.0.0.0", port=_port())`, where
  `_port()` reads `PORT` (default `5000`).
- **Routes:** `/` (dashboard), `/users`, `/broadcast`, `/api/data`,
  `/api/broadcast/status`, `/api/users/search`, `/api/users/block`, `/health`.
- **Auth:** HTTP Basic (`DASHBOARD_USER` / `DASHBOARD_TOKEN`), constant-time
  compare. `/health` is the only unauthenticated route.
- **Data source:** the dashboard does **not** read SQLite. It calls the bot
  worker's stats server (`stats_server.py`) over HTTP at `BOT_API_URL`
  (default `http://worker.railway.internal:8888`). See `ARCHITECTURE.md` →
  processes.
- **Database dependency:** indirect — SQLite is owned by the worker; the
  dashboard depends only on the worker's HTTP stats server being reachable.

## Topology (recommended, matches the repo)

Two processes from one repo (`Procfile`), sharing nothing but the private
network. The worker owns the DB volume; the dashboard is stateless.

```
worker (main.py)  ──►  SQLite volume (BOT_DB_DIR)
      │  runs stats_server.py on 0.0.0.0:STATS_PORT (8888, private)
      ▼
  private network (BOT_API_URL)
      ▲
web (dashboard.py)  ──►  serves the dashboard on 0.0.0.0:PORT (public)
```

The dashboard **can and should run as a separate service** from the worker; it
only needs `BOT_API_URL` to reach the worker and `DASHBOARD_TOKEN` to auth.

## Run commands (derived from `Procfile`)

| Process | Command |
|---|---|
| Bot worker | `python main.py` |
| Dashboard (web) | `python dashboard.py` |

**Local dashboard run:**
```
PORT=5000 DASHBOARD_TOKEN=<token> BOT_API_URL=http://localhost:8888 python dashboard.py
```
Open `http://localhost:5000/` and log in with `admin` / `<token>`.

**Production (Railway):** the `web` service runs `python dashboard.py` (from the
`Procfile`). It binds `0.0.0.0:$PORT` automatically — no code change needed.

## Required environment variables

| Variable | Used by | Required | Default | Notes |
|---|---|---|---|---|
| `DASHBOARD_TOKEN` | dashboard + stats server | **Yes** | — | Basic-auth password AND the stats-server token. Without it, authed routes return 503 (fail-closed). |
| `BOT_API_URL` | dashboard | Prod | `http://worker.railway.internal:8888` | Base URL of the worker's stats server. |
| `DASHBOARD_USER` | dashboard | No | `admin` | Basic-auth username. |
| `STATS_URL` | dashboard | No | `$BOT_API_URL/stats` | Override only if the stats path differs. |
| `PORT` | dashboard | Prod (platform-set) | `5000` | Bind port; Railway/Heroku inject it. |
| `STATS_PORT` | worker (stats server) | No | `8888` | Port the worker's stats server listens on. |
| `BOT_DB_DIR` | worker | Prod | `.` | SQLite dir — **must be a Railway volume** or data is lost on redeploy (`ARCHITECTURE.md`). |

Secrets live only in the platform env — never in the repo (`.env.example` holds
placeholders only; `CLAUDE.md`).

## Health check

Point the web service's health check at **`/health`** (open, returns `ok`).
Do **not** point it at `/` — that route requires auth and returns `401`, which
would mark the deploy unhealthy and keep it from receiving traffic. This is the
most common cause of a "dashboard is down" report when the code itself is fine.

## Recovery checklist (why the site can appear unavailable)

1. Web service exists and runs `python dashboard.py` (separate from the worker).
2. Health check path is `/health`, not `/`.
3. `DASHBOARD_TOKEN` is set on the web service (else login is impossible → 503).
4. `BOT_API_URL` points at a reachable worker (else `/` degrades to a safe 503
   placeholder — the dashboard stays up, but shows no data).
5. `PORT` is provided by the platform; the app binds `0.0.0.0:$PORT`.
6. The worker's SQLite lives on a persistent volume (`BOT_DB_DIR`).

## Related

- `ARCHITECTURE.md` → processes, deploy.
- Engineering System (when merged): `docs/engineering_system/goals/dashboard.md`,
  `docs/engineering_system/playbooks/dashboard.md`,
  `docs/engineering_system/playbooks/railway.md`.
