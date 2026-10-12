"""Ответ модели: полномочия органа, форма акта и ограничения проверки."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

AnswerValue = Literal["compliant", "violated", "not_established"]
Decision = Literal["administrative_regulation", "substantive_rule", "individual_decision", "amendment", "repeal", "other"]


class AuthorityAssessment(BaseModel):
    """Вывод о полномочиях и блоки текста, на которых он основан."""

    model_config = ConfigDict(extra="forbid")

    project_decision: Decision
    governed_decision: Decision
    project_evidence: list[StrictStr] = Field(min_length=1)
    norm_evidence: list[StrictStr]
    issuer_link_evidence: list[StrictStr] = Field(min_length=1)
    counterevidence: list[StrictStr]
    resolution: str
    answer: AnswerValue
    violation_scope: Literal["act", "provisions"] | None = None
    explanation: str
    recommendation: str = ""


class FormAssessment(BaseModel):
    """Допустимость формы акта с обоснованием и ссылками на блоки текста."""

    model_config = ConfigDict(extra="forbid")

    answer: AnswerValue
    explanation: str
    recommendation: str = ""
    evidence: list[StrictStr]


class Limitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Literal["authority", "form", "context", "input"]
    text: str = Field(min_length=1)


class CompetenceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authority: AuthorityAssessment
    form: FormAssessment
    limitations: list[Limitation] = Field(default_factory=list)
