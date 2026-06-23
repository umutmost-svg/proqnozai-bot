#!/bin/bash
# SessionStart hook for Claude Code on the web.
# Installs Python dependencies so test_e2e.py can run in a fresh container.
set -euo pipefail

# Only run in the remote (web) environment; locally the user manages their own venv.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# Project dependencies (python-telegram-bot, anthropic, httpx).
pip install -q -r requirements.txt

# cffi is required by cryptography's rust bindings; python-telegram-bot imports
# cryptography at module load, so without cffi `import telegram` panics.
pip install -q cffi

echo "session-start: dependencies installed"
