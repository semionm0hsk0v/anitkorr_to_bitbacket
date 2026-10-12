#!/usr/bin/env sh
set -eu

exec python -m uvicorn src.app.api.api:app \
    --host 0.0.0.0 \
    --port "${PORT:-8080}" \
    --workers "${WORKERS:-1}"
