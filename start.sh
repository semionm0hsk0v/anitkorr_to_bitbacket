#!/usr/bin/env sh
set -eu

APP_MODULE="${APP_MODULE:-src.app.api.api:app}"
HOST="${HOST:-0.0.0.0}"
PORT="${API_PORT:-8000}"

VENV_BIN="${VENV_BIN:-/app/.venv/bin}"
PYTHON_BIN="${PYTHON_BIN:-$VENV_BIN/python}"

if [ -x "$PYTHON_BIN" ]; then
  exec "$PYTHON_BIN" -m uvicorn "$APP_MODULE" \
    --host "$HOST" \
    --port "$PORT" \
    --proxy-headers \
    --server-header
fi

exec python -m uvicorn "$APP_MODULE" \
  --host "$HOST" \
  --port "$PORT" \
  --proxy-headers \
  --server-header
