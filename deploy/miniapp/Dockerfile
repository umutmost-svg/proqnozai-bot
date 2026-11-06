# Spor AI Mini App stand: built frontend + API in ONE image, ONE HTTPS origin.
# Separate from the bot's root Dockerfile; never used by the production bot.
# Build context = repository root (the root .dockerignore drops secrets, DBs, node_modules, dist):
#   docker build -f deploy/miniapp/Dockerfile -t sporai-miniapp .
# Base images are pinned by digest (multi-arch index); bump tag and digest together.

FROM node:24.22.2-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS frontend
ENV CI=1 npm_config_fund=false npm_config_update_notifier=false
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
# Exactly the lock file; fails if package.json and the lock disagree.
RUN npm ci
COPY frontend/ ./
# tsc + vite build, then the bundle scan for secrets and temporary URLs.
RUN npm run build && npm run check:bundle

FROM python:3.13.18-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MINIAPP_STATIC_DIR=/app/static \
    PORT=8000
RUN groupadd --system --gid 10001 miniapp \
 && useradd --system --uid 10001 --gid miniapp --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin miniapp
WORKDIR /app
COPY miniapp/requirements.lock miniapp/requirements.lock
# Hash-checked, wheels only, no resolver surprises: the lock is the whole dependency set.
RUN pip install --require-hashes --no-deps --only-binary=:all: -r miniapp/requirements.lock
# Application code only: no tests, no .env, no bot modules.
COPY miniapp/__init__.py miniapp/admin.py miniapp/app.py miniapp/asgi.py miniapp/security.py \
     miniapp/settings.py miniapp/store.py miniapp/telegram_auth.py miniapp/
COPY --from=frontend /build/frontend/dist /app/static
COPY deploy/miniapp/entrypoint.sh /usr/local/bin/miniapp-entrypoint
RUN chmod 0755 /usr/local/bin/miniapp-entrypoint && python -m compileall -q /app/miniapp
EXPOSE 8000
# Local docker only; Railway uses its own deploy-time healthcheck on /api/health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=4)"
# Starts as root only to hand the (root-owned) volume to the app user; serves as `miniapp`.
ENTRYPOINT ["/usr/local/bin/miniapp-entrypoint"]
CMD ["serve"]
