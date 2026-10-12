"""Совместная сверка актов и допуск её выводов в правовое заключение."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from pydantic import ValidationError

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.llm import llm
from src.agents.llm.errors import LLMGenerationError
from src.agents.prompts import describe_prompt, get_prompt
from src.agents.schemas.common.review import PROJECT_SOURCE_ID, Check, Citation, Review, Risk
from src.agents.schemas.legal.sources import LegalReading
from src.agents.schemas.legal.verdict import BlockResult, ComplianceIssue, LegalVerdict, SourceBlock
from src.agents.steps_tools.document.references import canonical_reference_address
from src.agents.steps_tools.review.failures import failure_reason
from src.agents.steps_tools.review.quote_locations import QuoteIndex
from src.agents.steps_tools.review.settle import settle
from src.common.config.config import settings

_MARKER = re.compile(r"(\d{1,2}\)|[а-яё]\))")


def build_source_blocks(items: Sequence[LegalReading]) -> list[SourceBlock]:
    """Полные прочитанные акты без повторов для совместной сверки."""
    blocks: list[SourceBlock] = []
    seen: set[str] = set()
    for item in items:
        raw = item.reading.raw
        if not item.reading.has_text or raw is None or item.reading.source_id in seen:
            continue
        seen.add(item.reading.source_id)
        blocks.append(SourceBlock(item=item, text=str(raw.full_text)))
    return blocks


def issue_to_risk(issue: ComplianceIssue) -> Risk:
    return Risk(
        kind=RiskKind.COMPLIANCE,
        explanation=" ".join(issue.explanation.split()),
        recommendation="Привести положение проекта в соответствие с вышестоящей нормой.",
        citations=[
            Citation(source=PROJECT_SOURCE_ID, quote=issue.project_quote),
            Citation(source=issue.source_id, quote=issue.source_quote),
        ],
    )


async def check_source_blocks(
    prompt_context: Mapping[str, str],
    blocks: Sequence[SourceBlock],
    *,
    timeout_s: float,
    related_sources: str = "",
    role: Literal["legal_verdict", "legal_scope", "legal_refusal"] = "legal_verdict",
) -> list[BlockResult]:
    """Сверить все прочитанные акты совместно, сохранив привязку выводов к источникам."""
    template = get_prompt(role)
    messages = template.compile(
        **prompt_context,
        sources="\n\n".join(block.render() for block in blocks),
        related_sources=related_sources or "Другие прочитанные источники отсутствуют.",
    )
    try:
        value = (
            await llm.generate_structured(
                role=role,
                messages=messages,
                prompt=describe_prompt(template),
                schema=LegalVerdict,
                timeout_s=timeout_s,
                operation=role,
                batch_id="legal-all-sources" if role == "legal_verdict" else f"{role}-all-sources",
                retries=max(0, int(settings[f"llm.{role}.max_retries"])),
            )
        ).value
    except LLMGenerationError as exc:
        return [BlockResult(block=block, verdict=None, exception=exc) for block in blocks]
    ids = {block.source_id for block in blocks}
    limitation = ""
    if any(issue.source_id not in ids for issue in value.issues):
        limitation = "Вывод ссылается на акт вне основного блока; вывод не включён."
    return [
        BlockResult(
            block=block,
            verdict=LegalVerdict(
                issues=[issue for issue in value.issues if issue.source_id == block.source_id],
            ),
            limitation=limitation,
        )
        for block in blocks
    ]


def assemble_compliance_review(
    results: Sequence[BlockResult],
    limitations: Sequence[str],
    *,
    document: str,
    sources: Mapping[str, str],
) -> Review:
    """Подтвердить цитаты сверки, объединить дубли и определить полноту."""
    titles = {result.block.source_id: result.block.title for result in results}
    risks: list[Risk] = []
    reasons = list(limitations)
    completed = 0
    indexes: dict[str, QuoteIndex] = {}

    def locate_citations(risk: Risk) -> None:
        for citation in risk.citations:
            text = document if citation.source == PROJECT_SOURCE_ID else sources.get(citation.source, "")
            if citation.source not in indexes:
                indexes[citation.source] = QuoteIndex(text)
            index = indexes[citation.source]
            citation.quote = index.restore_letter_spacing(citation.quote)
            spans = index.spans(citation.quote)
            # Missing quotes still reach settle, which accounts for rejected evidence.
            if spans:
                location = "; ".join(index.location(span) for span in spans)
                if len(spans) > 1:
                    location = f"Вхождения цитаты: {location}"
                if citation.source != PROJECT_SOURCE_ID:
                    location = f"{titles.get(citation.source, citation.source)}, {location}"
                citation.location = location

    for result in results:
        act = result.block.title.split("(ред.", 1)[0].strip()
        if result.verdict is None:
            exc = result.exception or TimeoutError()
            reasons.append(settings["agents.legal.verdict_review.unit_failed"].format(acts=act, reason=failure_reason(exc)))
            continue
        completed += 1
        for issue in result.verdict.issues:
            if issue.source_id != result.block.source_id:
                reasons.append("Вывод ссылается на акт вне основного блока; вывод не включён.")
                continue
            try:
                risk = issue_to_risk(issue)
            except ValidationError:
                reasons.append(f"Вывод по акту {act} содержит неполные обязательные данные; вывод не включён.")
                continue
            locate_citations(risk)
            risks.append(risk)
        if text := " ".join(result.limitation.split()):
            reasons.append(settings["agents.legal.verdict_review.unit_limitation"].format(acts=act, text=text))
    if not completed:
        status = CheckStatus.NOT_CHECKED
    elif reasons:
        status = CheckStatus.LIMITED
    else:
        status = CheckStatus.COMPLETED
    review = settle(
        Review(findings=risks, checks=[Check(kind=RiskKind.COMPLIANCE, status=status, reason=" ".join(dict.fromkeys(reasons)))]),
        (RiskKind.COMPLIANCE,),
        document=document,
        sources=sources,
    )
    return Review(findings=one_per_quote_and_act(review.findings, document=document), checks=review.checks)


def _split_item_marker(quote: str) -> tuple[str, str]:
    text = " ".join(quote.split())
    marker = _MARKER.match(text)
    if marker is None or not (rest := text[marker.end() :].lstrip()):
        return "", text
    return marker.group(1), rest


def _fold(text: str) -> str:
    return " ".join(text.replace("ё", "е").replace("Ё", "Е").split()).casefold()


def _place(location: str) -> str:
    "Адрес места проекта для сравнения: регистр, пробелы и знаки по краям слов не различают «пункт 2.1 подпункт 4»"
    return canonical_reference_address(location) or " ".join(word.strip(",.;:()«»") for word in location.casefold().split())


def one_per_quote_and_act(risks: Iterable[Risk], document: str = "") -> list[Risk]:
    "Удалять только дубли с теми же основаниями и обоснованием; разные дефекты сохранять."
    items = [(risk, *_split_item_marker(risk.citations[0].quote), risk.citations[-1].source) for risk in risks]
    markers: dict[tuple[str, str], set[str]] = {}
    for _, marker, rest, source in items:
        if marker:
            markers.setdefault((rest, source), set()).add(marker)
    folded = _fold(document)
    groups: dict[tuple, Risk] = {}
    for risk, marker, rest, source in items:
        ambiguous = len(markers.get((rest, source), ())) > 1
        place = ""
        core = _fold(rest).rstrip(" ;.,:")
        if core and folded.count(core) > 1:
            place = _place(risk.citations[0].location) or f"unknown:{id(risk)}"
        evidence = tuple(
            sorted(
                (c.source, _place(c.location), " ".join(c.quote.split())) for c in risk.citations if c.source != PROJECT_SOURCE_ID
            )
        )
        meaning = (" ".join(risk.explanation.split()), " ".join(risk.recommendation.split()))
        groups.setdefault((risk.kind, rest, source, marker if ambiguous else "", place, evidence, meaning), risk)
    return list(groups.values())
