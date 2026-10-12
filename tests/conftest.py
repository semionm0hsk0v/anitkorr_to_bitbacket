from __future__ import annotations

import pytest

from src.common.config.config import settings


@pytest.fixture(autouse=True, scope="session")
def _offline_runtime_boundaries():
    """Disable live LLM/gRPC access and provide deterministic startup settings once."""
    from src.agents.llm import (
        retry as llm_invoke,
    )
    from src.agents.steps_tools.sources import card_search

    patch = pytest.MonkeyPatch()
    patch.setattr(llm_invoke, "_retry_wait_s", lambda _index, _waits: 0.0)

    settings.set("LANGFUSE_PUBLIC_KEY", "")
    settings.set("LANGFUSE_SECRET_KEY", "")
    settings.set("BASE_MCP_URL", "")
    settings.set("ROUTER_AI_API_KEY", "test-router-ai-key-not-a-real-secret")
    settings.set("ROUTER_AI_BASE_URL", "https://routerai.invalid/api/v1")
    settings.set("S3_SERVICE_BASE_URL", "https://s3-service.invalid/")
    settings.set("S3_SERVICE_ACCESS_TOKEN", "test-s3-token-not-a-real-secret")
    card_search._TRANSPORT["tool"] = card_search._UNPROBED
    yield
    card_search._TRANSPORT["tool"] = card_search._UNPROBED
    patch.undo()


@pytest.fixture(autouse=True)
def _reset_card_search_cache():
    """Prevent a transport selected by one test from leaking into the next."""
    yield
    from src.agents.steps_tools.sources import card_search

    card_search._TRANSPORT["tool"] = card_search._UNPROBED
