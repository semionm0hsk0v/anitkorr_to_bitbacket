FROM cr.yandex/crpel251bjgcjbhtu439/infrastruction/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH=/app/.venv/bin:$PATH

ARG USER_ID=10001
ARG GROUP_ID=10001

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && addgroup --system --gid "${GROUP_ID}" appgroup \
    && adduser --system --uid "${USER_ID}" --ingroup appgroup appuser \
    && pip install --no-cache-dir uv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN chown appuser:appgroup /app

COPY --chown=appuser:appgroup pyproject.toml uv.lock README.md ./
USER appuser
RUN uv sync --frozen --no-dev

COPY --chown=appuser:appgroup src ./src
COPY --chown=appuser:appgroup --chmod=755 ci_cd/run_scripts/start.sh ./start.sh

CMD ["/app/start.sh"]
