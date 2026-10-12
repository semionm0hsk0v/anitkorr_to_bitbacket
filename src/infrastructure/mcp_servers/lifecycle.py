from __future__ import annotations

from src.common.config.config import settings
from src.infrastructure.mcp_servers.client import mcp_client


def mcp_is_enabled() -> bool:
    """MCP доступен, если задан базовый URL."""
    return bool(settings.get("BASE_MCP_URL"))


async def init_mcp_if_enabled() -> None:
    if not mcp_is_enabled():
        return
    if mcp_client._client is not None:
        return
    await mcp_client.init()
