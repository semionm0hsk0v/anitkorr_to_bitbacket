from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from src.agents.schemas.common.review import Review


class BranchAccountingDelta(TypedDict, total=False):
    "Общая часть выхода любой ветви: её собственный учёт токенов."

    token_usage_incomplete: Annotated[bool, operator.or_]
    token_usage_total: Annotated[int, operator.add]
    prompt_tokens_total: Annotated[int, operator.add]
    completion_tokens_total: Annotated[int, operator.add]
    cache_read_tokens_total: Annotated[int, operator.add]


class ComplianceBranchOutput(BranchAccountingDelta, total=False):
    """Выход трёх нод сверки без промежуточных кандидатов и прочитанных текстов."""

    legal_review: Review


class LegalBranchOutput(ComplianceBranchOutput, total=False):
    """Что ветвь «детектор отмены ∥ сверка с вышестоящими актами» отдаёт наверх."""

    repeal_review: Review
