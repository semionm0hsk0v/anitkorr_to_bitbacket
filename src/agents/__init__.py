"""Lazy access to the compiled graph; Generation lives in agents.llm; provider transport lives in infrastructure.llm."""

import functools


@functools.lru_cache(maxsize=1)
def _get_antikorr_app():
    from .graph import build_graph  # noqa: PLC0415 - lazy graph construction

    return build_graph().compile()


def __getattr__(name: str):
    if name == "antikorr_app":
        return _get_antikorr_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
