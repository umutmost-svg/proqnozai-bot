# SporBot admin panel, SERVER mode (docs/ops/admin-server.md): built admin frontend + the admin API in one image.
# Build context = repository root:
#   docker build -f deploy/admin/Dockerfile -t sporbot-admin .
# Same pinned bases and hash-checked lock as the Mini App image.

# The frontend stage runs on the BUILD machine's platform: its output is static files, the same on any platform, and
# `npm run build` under amd64 emulation on an arm64 Mac crashes (exit 139, 17.11).
FROM --platform=$BUILDPLATFORM node:24.22.2-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS frontend
ENV CI=1 npm_config_fund=false npm_config_update_notifier=false
WORKDIR /build/admin-frontend
COPY admin-frontend/package.json admin-frontend/package-lock.json ./
RUN npm ci
COPY admin-frontend/ ./
RUN npm run build

FROM python:3.13.18-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ADMIN_WEB_MODE=server \
    ADMIN_WEB_STATIC_DIR=/app/static \
    ADMIN_WEB_PORT=8795
# The Mini App's uid (10001): the admin reads its SQLite files (WAL needs write access to -shm) on the shared volume.
RUN groupadd --system --gid 10001 miniapp \
 && useradd --system --uid 10001 --gid miniapp --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin miniapp
WORKDIR /app
COPY miniapp/requirements.lock miniapp/requirements.lock
RUN pip install --require-hashes --no-deps --only-binary=:all: -r miniapp/requirements.lock
COPY miniapp/ miniapp/
# Application code only: no tests, no local pilot data, no caches.
RUN rm -rf miniapp/tests miniapp/.venv miniapp/**/__pycache__ && find /app/miniapp -name "*.db" -delete
COPY --from=frontend /build/admin-frontend/dist /app/static
COPY deploy/admin/entrypoint.sh /usr/local/bin/admin-entrypoint
RUN chmod 0755 /usr/local/bin/admin-entrypoint && python -m compileall -q /app/miniapp
EXPOSE 8795
# The health endpoint answers only with the edge secret and the public host, like every request.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8795/api/admin/health', headers={'Host': os.environ['ADMIN_PUBLIC_HOST'], 'X-Admin-Edge-Auth': os.environ['ADMIN_EDGE_SECRET']}), timeout=4)"
ENTRYPOINT ["/usr/local/bin/admin-entrypoint"]
CMD ["serve"]
