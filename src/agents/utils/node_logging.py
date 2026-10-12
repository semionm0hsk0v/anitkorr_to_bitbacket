from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable

from src.common.utils.logger import get_logger
from src.infrastructure.llm.errors import llm_error_log_fields

log = get_logger("src.agents.pipeline")


def logged_node[StateT: dict](
    fn: Callable[[StateT], Awaitable[dict | None]],
    *,
    name: str,
) -> Callable[[StateT], Awaitable[dict | None]]:
    """Оборачивает узел графа структурным логом жизненного цикла (см. модульный docstring)."""

    @functools.wraps(fn)
    async def wrapper(state: StateT) -> dict | None:
        await log.adebug("node_start", node=name)
        try:
            result = await fn(state)
        except Exception as exc:
            await log.aerror(
                "node_error",
                node=name,
                error_class=type(exc).__name__,
                **llm_error_log_fields(exc),
            )
            raise
        await log.ainfo("node_done", node=name)
        return result

    return wrapper
