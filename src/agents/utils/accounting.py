import functools
from collections.abc import Awaitable, Callable

from src.agents.llm.usage import current_usage_snapshot, interrupted_usage, open_token_scope, reset_token_scope
from src.common.utils.logger import get_logger

log = get_logger(__name__)


def accounted_node[StateT: dict](
    fn: Callable[[StateT], Awaitable[dict | None]],
    *,
    stage: str | None = None,
    usage_channel: str | None = None,
) -> Callable[[StateT], Awaitable[dict]]:
    @functools.wraps(fn)
    async def wrapper(state: StateT) -> dict:
        scope = open_token_scope()
        try:
            out = dict(await fn(state) or {})
            snapshot = current_usage_snapshot()
            if snapshot.usage is not None:
                for channel, value in (
                    ("token_usage_total", snapshot.usage.total_tokens),
                    ("prompt_tokens_total", snapshot.usage.input_tokens),
                    ("completion_tokens_total", snapshot.usage.output_tokens),
                    ("cache_read_tokens_total", snapshot.usage.cache_read_tokens),
                ):
                    if value is not None:
                        out[channel] = value
            if not snapshot.complete:
                out["token_usage_incomplete"] = True
            if stage is not None and usage_channel is not None:
                out[usage_channel] = {stage: snapshot}
            return out
        except BaseException as exc:
            diagnostic = interrupted_usage()
            # Keep safe attempt metadata available to the request owner without
            # converting cancellation into a graph result or committing state.
            exc.llm_diagnostics = diagnostic  # type: ignore[attr-defined]  # safe metadata on the original exception
            log.warning(
                "node_usage_interrupted",
                node=stage or fn.__name__,
                tokens=diagnostic.usage.total_tokens if diagnostic.usage is not None else None,
                attempts=len(diagnostic.attempts),
                usage_complete=False,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            reset_token_scope(scope)

    return wrapper
