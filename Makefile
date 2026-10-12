.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test compile check diff-check run

help:
	@grep -E '^[a-z][a-zA-Z0-9_-]*:.*##' $(MAKEFILE_LIST) | awk -F':.*##' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## uv sync --frozen (зависимости строго из uv.lock)
	uv sync --frozen

lint: ## ruff format --check + ruff check
	uv run ruff format --check src tests
	uv run ruff check src tests

format: ## ruff format + автофиксы (не входит в check)
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck: ## mypy
	uv run mypy src

test: ## pytest
	uv run pytest -q

compile: ## синтаксис всего src
	uv run python -m compileall -q src

check: lint typecheck test compile ## всё, что проверяет CI

diff-check: ## гигиена пробелов в рабочем дереве (перед коммитом)
	git diff --check

run: ## локальный API: MCP /mcp, живость /health
	uv run python -m src.app.run
