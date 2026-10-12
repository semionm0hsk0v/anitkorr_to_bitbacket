from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.agents.steps_tools.document.act_basis import extract_act_basis
from src.common import IntakeError
from src.common.config.config import settings

#: Сколько символов от начала считаем «шапкой» документа.
_HEAD_CHARS = int(settings["app.intake.head_chars"])
#: Максимум файлов за одну экспертизу (проект + пояснительная + запас).
MAX_INTAKE_FILES = int(settings["app.intake.max_intake_files"])

_NOTE_FILENAME_RE = re.compile(r"пояснит|поясн|обоснован|фэо|записка", re.IGNORECASE)
_NOTE_HEADING_RE = re.compile(
    r"пояснительн\w*\s+записк|финансово[\s-]*экономическ\w*\s+обоснован|обоснование\s+необходимости",
    re.IGNORECASE,
)
_NOTE_BODY_RE = re.compile(
    r"оценк\w*\s+регулирующего\s+воздействия|разработан\w*\s+в\s+целях|проект\w*\s+подготовлен",
    re.IGNORECASE,
)

_ACT_HEADING_RE = re.compile(
    r"(?m)^\s*(приказ|постановление|распоряжение|решение|указ|закон)\s*$",
    re.IGNORECASE,
)
_NUMBERED_ITEM_RE = re.compile(r"(?m)^\s*\d+(?:\.\d+)*\.\s+\S")
_NORMATIVE_PHRASE_RE = re.compile(
    r"утвердить\s+прилагаем|призна\w*\s+утративш\w*\s+силу|вступает\s+в\s+силу|внести\s+в\s+.{0,80}измен",
    re.IGNORECASE,
)

#: Порог «это проект НПА»: даёт только формула издания (3) либо шапка+пункты (2+2).
_PROJECT_THRESHOLD = int(settings["app.intake.project_threshold"])
#: Порог «это пояснение»: заголовок (3) либо имя файла (2) либо обороты записки (1+…).
_NOTE_THRESHOLD = int(settings["app.intake.note_threshold"])


@dataclass(frozen=True)
class IntakeDocument:
    """Один загруженный и уже распарсенный файл."""

    name: str
    text: str
    source_document_id: str | None = None
    table_geometry: dict | None = field(default=None, compare=False)

    @property
    def project_score(self) -> int:
        """Признаки нормативного акта (чем больше — тем увереннее «это проект»)."""
        text = self.text or ""
        score = 0
        # Формула издания («п р и к а з ы в а ю:») — тот же экстрактор, что у стадии 1.
        if extract_act_basis(text) is not None:
            score += 3
        head = text[:_HEAD_CHARS]
        if _ACT_HEADING_RE.search(head):
            score += 2
        if len(_NUMBERED_ITEM_RE.findall(text)) >= 5:
            score += 2
        if _NORMATIVE_PHRASE_RE.search(text):
            score += 1
        return score

    @property
    def note_score(self) -> int:
        """Признаки пояснительной записки (заголовок / имя файла / обороты)."""
        score = 0
        if _NOTE_HEADING_RE.search((self.text or "")[:_HEAD_CHARS]):
            score += 3
        if _NOTE_FILENAME_RE.search(self.name or ""):
            score += 2
        if _NOTE_BODY_RE.search(self.text or ""):
            score += 1
        return score

    @property
    def looks_like_project(self) -> bool:
        return self.project_score >= _PROJECT_THRESHOLD

    @property
    def looks_like_note(self) -> bool:
        "Пояснение — только при ОТСУТСТВИИ нормативной структуры."
        return self.note_score >= _NOTE_THRESHOLD and not self.looks_like_project


@dataclass(frozen=True)
class IntakeDecision:
    """Кто предмет экспертизы, кто контекст, и насколько мы в этом уверены."""

    subject: IntakeDocument
    notes: list[IntakeDocument] = field(default_factory=list)
    #: True — роли распознаны по содержимому; False — взяли больший файл «вслепую».
    confident: bool = True
    #: Текст для интерфейса (пусто — сообщать нечего, загружен один файл).
    message: str = ""

    @property
    def note_names(self) -> list[str]:
        return [d.name for d in self.notes]


def _quoted(names: list[str]) -> str:
    return ", ".join(f"«{n}»" for n in names)


def _confident_message(subject: IntakeDocument, notes: list[IntakeDocument]) -> str:
    return (
        f"Предмет экспертизы — «{subject.name}».\n"
        f"{_quoted([d.name for d in notes])} — пояснение к проекту: "
        "учитывается как справочный материал, замечания формируются только по тексту проекта."
    )


def _unsure_message(subject: IntakeDocument, notes: list[IntakeDocument]) -> str:
    return (
        "Не удалось однозначно определить, какой файл является проектом НПА. "
        f"Предметом экспертизы взят «{subject.name}» (самый объёмный из загруженных). "
        f"Остальные файлы ({_quoted([d.name for d in notes])}) учтены как пояснения. "
        "Если это не так — загрузите проект отдельным запросом."
    )


def classify_intake(docs: list[IntakeDocument]) -> IntakeDecision:
    "Разложить загруженные файлы на предмет экспертизы и пояснения."
    if not docs:
        raise ValueError("classify_intake: пустой список документов")
    if all(d.looks_like_note for d in docs):
        raise IntakeError("Приложена только пояснительная записка. Приложите обязательный проект НПА вместе с пояснением.")
    if len(docs) == 1:
        return IntakeDecision(subject=docs[0], notes=[], confident=True, message="")

    projects = [d for d in docs if d.looks_like_project]
    if len(projects) == 1:
        subject = projects[0]
        notes = [d for d in docs if d is not subject]

        confident = all(d.looks_like_note for d in notes)
        message = _confident_message(subject, notes) if confident else _unsure_message(subject, notes)
        return IntakeDecision(subject=subject, notes=notes, confident=confident, message=message)

    # Ноль или несколько «проектов» → распознать не удалось: больший/первый файл.
    pool = projects or docs
    subject = max(pool, key=lambda d: len(d.text or ""))
    notes = [d for d in docs if d is not subject]
    return IntakeDecision(
        subject=subject,
        notes=notes,
        confident=False,
        message=_unsure_message(subject, notes),
    )
