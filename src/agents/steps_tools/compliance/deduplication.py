"""Объединить смысловые повторы, сохранив исходные выводы и доказательства."""

import json

from src.agents.enums.common.review import CheckStatus
from src.agents.llm import llm
from src.agents.llm.errors import LLMGenerationError
from src.agents.prompts import describe_prompt, get_prompt
from src.agents.schemas.common.review import Review, Risk
from src.agents.schemas.legal.deduplication import FindingGroups
from src.agents.steps_tools.review.settle import worse
from src.common.config.config import settings


def grouped_findings(findings: list[Risk], result: FindingGroups) -> list[Risk]:
    """Полное непересекающееся покрытие; тексты и цитаты берутся только из входа."""
    members = [i for group in result.groups for i in group.members]
    if sorted(members) != list(range(1, len(findings) + 1)) or any(
        group.representative not in group.members for group in result.groups
    ):
        raise ValueError("Группы не покрывают исходные находки ровно по одному разу.")
    merged = []
    for group in sorted(result.groups, key=lambda g: min(g.members)):
        representative = findings[group.representative - 1]
        ordered = [group.representative, *sorted(i for i in group.members if i != group.representative)]
        citations = []
        seen = set()
        for index in ordered:
            risk = findings[index - 1]
            if risk.kind is not representative.kind:
                raise ValueError("Группа объединяет разные направления проверки.")
            for citation in risk.citations:
                identity = (citation.source, citation.location, citation.quote)
                if identity not in seen:
                    seen.add(identity)
                    citations.append(citation)
        merged.append(representative.model_copy(update={"citations": citations}))
    return merged


async def merge_duplicate_findings(review: Review) -> Review:
    """При сбое группировки сохранить все находки и обозначить ограничение."""
    if len(review.findings) < 2:
        return review
    template = get_prompt("legal_dedup")
    messages = template.compile(
        findings=json.dumps(
            [{"id": i, **risk.model_dump(mode="json")} for i, risk in enumerate(review.findings, 1)],
            ensure_ascii=False,
        )
    )
    try:
        result = await llm.generate_structured(
            role="legal_dedup",
            messages=messages,
            prompt=describe_prompt(template),
            schema=FindingGroups,
            timeout_s=float(settings["llm.legal_dedup.timeout"]),
            retries=max(0, int(settings["llm.legal_dedup.max_retries"])),
            operation="legal_dedup",
        )
    except LLMGenerationError:
        reason = "Смысловое объединение повторов не выполнено из-за сбоя модели; все исходные замечания сохранены."
    else:
        try:
            findings = grouped_findings(review.findings, result.value)
        except ValueError:
            reason = "Модель вернула некорректные группы повторов; все исходные замечания сохранены."
        else:
            return Review(findings=findings, checks=review.checks)
    return Review(
        findings=review.findings,
        checks=[
            check.model_copy(
                update={
                    "status": worse(check.status, CheckStatus.LIMITED),
                    "reason": " ".join(filter(None, (check.reason, reason))),
                }
            )
            for check in review.checks
        ],
    )
