"""Поиск карточек и чтение данных документов через MCP правовой базы."""

from __future__ import annotations

from typing import Any

from src.agents.schemas.legal_base.card_search import CardHit, CardQuery, ConsultantCapabilities
from src.agents.steps_tools.sources.normalization import card_date, card_number
from src.agents.steps_tools.sources.region_banks import known_banks
from src.agents.steps_tools.sources.transport_barrier import is_external_transport_failure
from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)


_DEFAULT_TOOLS = ("consultant_card_search", "CardSearch")

_UNPROBED = "?"


_TRANSPORT: dict[str, str | None] = {"tool": _UNPROBED}


def consultant_capabilities(sessions: Any) -> ConsultantCapabilities:
    """Resolve capabilities per operation instead of using session presence as a proxy."""
    supports = getattr(sessions, "supports", None)

    def mcp_has(tool_name: str) -> bool:
        if not callable(supports):
            return False
        try:
            return bool(supports("consultant", tool_name))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

    return ConsultantCapabilities(
        free_search=mcp_has("consultant_quick_search"),
        exact_card_search=any(mcp_has(name) for name in _tool_names()),
        doc_info=mcp_has("consultant_doc_info"),
        doc_text=mcp_has("consultant_get_document_text"),
    )


def _tool_names() -> tuple[str, ...]:
    configured = settings["mcp.card_search_tools"]
    if isinstance(configured, (list, tuple)) and configured:
        return tuple(str(t) for t in configured)
    return _DEFAULT_TOOLS


def _is_missing_tool(exc: BaseException) -> bool:
    "Ошибка «инструмента нет на сервере» (а не сетевой сбой): отличаем, чтобы не"
    msg = str(exc).lower()
    return "not found" in msg and "tool" in msg


def valid_banks(banks: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    "Отсечь имена банков, которых нет в карте: К+ на неизвестный срез отвечает"
    known = known_banks()
    return tuple(b for b in banks if b in known)


def _as_items(found: Any) -> list[dict[str, Any]]:
    """Ответ инструмента → список словарей (форма ответа у сборок kb-mcp разная)."""
    if isinstance(found, list):
        return [it for it in found if isinstance(it, dict)]
    if isinstance(found, dict):
        for key in ("documents", "results", "docs", "data"):
            value = found.get(key)
            if isinstance(value, list):
                return [it for it in value if isinstance(it, dict)]
    return []


def _to_hit(item: dict[str, Any]) -> CardHit | None:
    "Элемент выдачи → ``CardHit``. Принимаем и плоскую форму (``bank``/``doc_num``),"
    raw_label = item.get("doc_par_label")
    label: dict[str, Any] = raw_label if isinstance(raw_label, dict) else {}
    bank = item.get("bank") or item.get("base") or label.get("bank_name")
    doc_num = item.get("doc_num") or label.get("doc_num")
    if not bank or doc_num is None:
        return None
    try:
        return CardHit(bank=str(bank), doc_num=int(doc_num), title=str(item.get("title") or ""))
    except (TypeError, ValueError):
        return None


async def _mcp_card_search(sessions: Any, query: CardQuery) -> list[CardHit] | None:
    """Карточный поиск через MCP. ``None`` — инструмента нет (или нет сессий)."""
    if sessions is None:
        return None
    cached = _TRANSPORT["tool"]
    if cached is None:  # уже выяснили: карточного поиска в MCP нет
        return None
    names = _tool_names() if cached == _UNPROBED else (cached,)
    for name in names:
        try:
            found = await sessions.invoke("consultant", name, query.payload(), timeout_s=60)
        except Exception as exc:
            if _is_missing_tool(exc):
                continue  # такого инструмента нет — пробуем следующее имя
            if is_external_transport_failure(exc):
                raise  # A transport failure must not become a negative search result.
            await log.awarning("card_search_mcp_failed", tool=name, error_class=type(exc).__name__, detail=str(exc)[:200])

            return None
        _TRANSPORT["tool"] = name
        hits = [h for h in (_to_hit(it) for it in _as_items(found)) if h is not None]
        return hits
    _TRANSPORT["tool"] = None  # ни одно имя не сработало — карточного поиска в MCP нет
    await log.ainfo("card_search_mcp_absent", tried=list(names))
    return None


async def card_search(sessions: Any, query: CardQuery) -> list[CardHit] | None:
    "Карточный поиск по реквизитам."
    if query.is_empty():
        return None
    requested_banks = query.banks
    query = (query).model_copy(
        update={"number": card_number(query.number), "date": card_date(query.date), "banks": valid_banks(requested_banks)}
    )
    if query.is_empty():
        return None
    if requested_banks and not query.banks:
        await log.awarning("card_search_invalid_banks", banks=list(requested_banks))
        return []

    hits = await _mcp_card_search(sessions, query)
    if hits is not None:
        await log.adebug("card_search", transport="mcp", banks=list(query.banks), number=query.number, hits=len(hits))
        return hits
    return None


#: Как назван транспорт, обслуживший операцию, в currentness receipt и trace.
TRANSPORT_MCP = "consultant_mcp"


async def fetch_doc_info_with_transport(sessions: Any, bank: str, doc_num: int) -> tuple[dict[str, Any] | None, str | None]:
    """То же плюс имя фактически сработавшего транспорта (пишется в живые данные квитанции)."""
    if sessions is not None:
        try:
            info = await sessions.invoke(
                "consultant", "consultant_doc_info", {"doc_base": bank, "doc_num": doc_num}, timeout_s=60
            )
            return (info if isinstance(info, dict) else {}), TRANSPORT_MCP
        except Exception as exc:
            if is_external_transport_failure(exc):
                raise
            if not _is_missing_tool(exc):
                await log.awarning(
                    "doc_info_failed", base=bank, doc_num=doc_num, error_class=type(exc).__name__, detail=str(exc)[:200]
                )
            return None, None

    return None, None


TOOL_LINKS_TO_DOC = "consultant_get_links_to_doc"


async def fetch_links_to_doc(sessions: Any, bank: str, doc_num: int) -> list[tuple[str, int]] | None:
    "Документы, ссылающиеся на документ целиком (корреспонденты в терминах К+)."
    supports = getattr(sessions, "supports", None)
    if not callable(supports) or not supports("consultant", TOOL_LINKS_TO_DOC):
        return None
    try:
        found = await sessions.invoke(
            "consultant", TOOL_LINKS_TO_DOC, {"base": bank, "doc_num": doc_num, "label": -1}, timeout_s=60
        )
    except Exception as exc:
        if is_external_transport_failure(exc):
            raise
        if not _is_missing_tool(exc):
            await log.awarning(
                "links_to_doc_failed", base=bank, doc_num=doc_num, error_class=type(exc).__name__, detail=str(exc)[:200]
            )
        return None
    if not isinstance(found, list):
        await log.awarning("links_to_doc_unexpected_shape", base=bank, doc_num=doc_num, shape=type(found).__name__)
        return None
    links: list[tuple[str, int]] = []
    for item in _as_items(found):
        item_bank, item_num = item.get("base"), item.get("doc_num")
        if isinstance(item_bank, str) and isinstance(item_num, int) and (item_bank, item_num) not in links:
            links.append((item_bank, item_num))
    return links


async def fetch_doc_text_with_transport(
    sessions: Any, bank: str, doc_num: int, par_start: int, par_end: int
) -> tuple[str | None, str | None]:
    """То же плюс имя фактически сработавшего транспорта."""
    if sessions is not None:
        try:
            chunk = await sessions.invoke(
                "consultant",
                "consultant_get_document_text",
                {"base": bank, "doc_num": doc_num, "par_start": par_start, "par_end": par_end},
                timeout_s=90,
            )
            return (str(chunk.get("segment") or "") if isinstance(chunk, dict) else str(chunk or "")), TRANSPORT_MCP
        except Exception as exc:
            if is_external_transport_failure(exc):
                raise
            if not _is_missing_tool(exc):
                await log.awarning(
                    "doc_text_failed", base=bank, doc_num=doc_num, error_class=type(exc).__name__, detail=str(exc)[:200]
                )
            return None, None

    return None, None


async def quick_search(sessions: Any, query: str, *, limit: int = 10) -> list[CardHit] | None:
    """Предметный поиск через MCP; None означает недоступный инструмент."""
    if sessions is None:
        return None
    try:
        result = await sessions.invoke(
            "consultant",
            "consultant_quick_search",
            {"query": query, "max_results": limit, "get_title": True},
            timeout_s=60,
        )
    except Exception as exc:
        if _is_missing_tool(exc):
            return None
        raise
    return [hit for item in _as_items(result) if (hit := _to_hit(item)) is not None]
