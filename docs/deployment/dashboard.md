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

## Common deployment failures

The most frequent ways the dashboard "breaks" in production — and how to fix each.

### 1. Dashboard shows 503 after login

**Symptoms**

- Login succeeds.
- Dashboard loads.
- Data panel shows 503.
- Worker responds with: `dashboard token required`.

**Root cause**

The **worker** service does not have `DASHBOARD_TOKEN` configured. The dashboard
sends the token correctly, but the worker's `stats_server.py` has an empty
`STATS_TOKEN`, so it returns:

```
503 dashboard token required
```

instead of:

```
401 unauthorized
```

The status code is the tell: `/stats` returns **503 only when the worker's own
token is empty**; a token that is set but *mismatched* returns **401**. A 503
therefore means "worker has no token", not "wrong token".

**Resolution**

Set the **same** `DASHBOARD_TOKEN` value on **both** services:

- Dashboard (web)
- Worker (bot)

Then redeploy the worker.

**Expected verification**

```
GET /health            → 200
GET /stats?token=<token>  → 200
```

(`/health` returns 200 even without a token; `/stats` needs the matching token.)

### 2. Dashboard cannot start

Common causes:

- **`PORT` not bound** — the app must bind `0.0.0.0:$PORT`; a hardcoded or missing
  port means the platform can't route to it.
- **Wrong start command** — the web service must run `python dashboard.py` (not
  `python main.py`, which is the worker).
- **Web service not created** — the dashboard must exist as its **own** Railway
  service; a worker-only deploy has no public dashboard URL.

### 3. Dashboard opens but has no data

Common causes:

- **`BOT_API_URL` incorrect** — points somewhere other than the worker's stats
  server.
- **Worker unreachable** — the worker is down or not on the same private network.
- **Internal Railway hostname incorrect** — e.g. the `*.railway.internal` host or
  `STATS_PORT` does not match the worker.
- **Worker not running** — no stats server is listening to answer `/stats`.

### 4. Health check fails

The Railway health check must point to:

```
/health
```

**Never** use:

```
/
```

The root endpoint is protected with HTTP Basic Auth and returns **401** to an
unauthenticated probe, which marks the deploy unhealthy and stops it from
receiving traffic. `/health` is the only unauthenticated route and always
returns `200 ok`.

### Security note

If `DASHBOARD_TOKEN` has ever appeared in any of:

- browser errors
- logs
- screenshots
- chats
- recordings

treat it as **compromised**. Rotate the token immediately. The dashboard and the
worker must **both** receive the new value (set it on both services, then
redeploy the worker). The token doubles as the dashboard's Basic-Auth password
and the worker's stats-server token, so a leak exposes both.

## Related

- `ARCHITECTURE.md` → processes, deploy.
- Engineering System (when merged): `docs/engineering_system/goals/dashboard.md`,
  `docs/engineering_system/playbooks/dashboard.md`,
  `docs/engineering_system/playbooks/railway.md`.
