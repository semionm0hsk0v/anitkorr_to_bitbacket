#!/bin/sh
set -eu

exec python -m uvicorn src.app.api.api:app \
    --host 0.0.0.0 \
    --port "${PORT:-8080}" \
    --no-access-log \
    --no-server-header \
    --workers "${WORKERS:-1}" \
    --log-level "${LOG_LEVEL:-info}"
