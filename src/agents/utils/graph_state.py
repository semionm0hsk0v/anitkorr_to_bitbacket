from __future__ import annotations

from typing import Annotated, get_origin, get_type_hints

from src.agents.state import PipelineState


def _accumulating_channels() -> frozenset[str]:
    "Каналы с reducer-ом (``Annotated[..., operator.add]``) — вычисляются из схемы."
    hints = get_type_hints(PipelineState, include_extras=True)
    return frozenset(name for name, hint in hints.items() if get_origin(hint) is Annotated)


ACCUMULATING_CHANNELS: frozenset[str] = _accumulating_channels()
