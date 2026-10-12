from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.providers.skills import SkillsDirectoryProvider

from src.app.mcp.runner import run_expertise_stream
from src.app.mcp.schemas import AntikorrInvokeRequest
from src.infrastructure.llm.context import bind_identity, bind_skill_identity, identity_from_headers

#: src/app/mcp/server.py → parents[2] == src
SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"

mcp = FastMCP(
    name="antikorr",
    providers=[SkillsDirectoryProvider(roots=SKILLS_ROOT)],
)


@mcp.tool()
async def antikorr_invoke(data: AntikorrInvokeRequest, ctx: Context) -> dict[str, Any]:
    """Правовая экспертиза проекта НПА (antikorr).

    По приложенному проекту нормативного правового акта проверяет компетенцию органа,
    внутренние противоречия проекта (отсылки, состав приложений, наименование услуги,
    сроки, значения одной величины; смысловые противоречия пока не проверяются),
    соответствие вышестоящим актам в действующей редакции и полноту отмены заменяемых
    актов. Возвращает заключение по подтверждённым нарушениям: место в проекте,
    дословная цитата, применимая норма. Промежуточный ход проверки уходит
    уведомлениями MCP. Некорректный вход (нет проекта, нечитаемое
    вложение) отдаётся понятным ответом пользователю, а не ошибкой вызова инструмента.
    """

    # FastMCP выполняет tools/call в задаче сессии: контекст HTTP-мидлвари может устареть.
    message_id = str(data.message_id) if data.message_id else str(uuid.uuid4())
    bind_identity(identity_from_headers(get_http_headers()))
    bind_skill_identity(thread_id=str(data.thread_id), message_id=message_id)

    async def on_event(payload: dict[str, Any]) -> None:
        await ctx.info(json.dumps(payload, ensure_ascii=False, default=str), logger_name="antikorr.stream")

    return await run_expertise_stream(
        thread_id=str(data.thread_id),
        history_messages=data.history_messages,
        file_ids=data.file_ids,
        message_id=message_id,
        on_event=on_event,
    )


mcp_app = mcp.http_app(path="/", transport="http")
