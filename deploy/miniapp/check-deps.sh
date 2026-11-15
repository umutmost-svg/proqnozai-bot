#!/bin/sh
# Dependency check for the Mini App stand. Run from the repo root before every
# release and after any lock change. Needs network (PyPI, npm registry, OSV).
#   sh deploy/miniapp/check-deps.sh
# Fails on: a lock that no longer matches requirements*.txt, any known
# vulnerability in runtime dependencies (Python lock, npm production deps).
# Dev-only npm findings are printed but do not fail (they never ship in the image).
set -eu

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "== Python lock is current"
# Same commands that produced the locks (run inside miniapp/ so `# via` paths match).
(cd miniapp && uv pip compile requirements.txt --generate-hashes --universal --python-version 3.13 \
  --no-header -q -o "$tmp/requirements.lock" \
  && uv pip compile requirements-dev.txt -c requirements.lock --generate-hashes --universal \
  --python-version 3.13 --no-header -q -o "$tmp/requirements-dev.lock")
diff -u miniapp/requirements.lock "$tmp/requirements.lock"
diff -u miniapp/requirements-dev.lock "$tmp/requirements-dev.lock"

echo "== Python runtime vulnerabilities (pip-audit, OSV/PyPI)"
uvx --quiet pip-audit==2.9.0 -r miniapp/requirements.lock --require-hashes --disable-pip --progress-spinner off

echo "== npm lock matches package.json"
(cd frontend && npm ci --dry-run --ignore-scripts >/dev/null)

echo "== npm production vulnerabilities"
(cd frontend && npm audit --omit=dev --audit-level=low)

echo "== npm dev-only findings (informational)"
(cd frontend && npm audit --audit-level=critical) || true
