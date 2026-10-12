"""Отбор статей о полномочиях и подготовка источников в пределах контекста."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from src.agents.enums.competence.fragments import SourceMode
from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.enums.legal_base.source_refs import SourceRole
from src.agents.schemas.competence.fragments import SourceView
from src.agents.schemas.competence.sources import CompetenceRefs, CompetenceSources
from src.agents.schemas.legal_base.source_fragments import Article
from src.agents.schemas.legal_base.source_reading import SourceReading
from src.agents.schemas.parse.structure import DocumentStructureNode
from src.agents.steps_tools.document.structure import build_document_structure_tree
from src.infrastructure.knowledge.organ_statutes import OrganStatute, load_organ_statutes, mentions

_BOUNDARY_KINDS = frozenset({"article", "chapter", "section", "appendix"})


def is_authority_article(article: Article, issuer: OrganStatute | None) -> bool:
    "Признаки §7 (``authority_markers`` справочника)."
    markers = load_organ_statutes().authority_markers
    heading = article.heading.lower().replace("ё", "е")
    if any(stem in heading for stem in markers.heading_stems):
        return True
    if any(mentions(anchor, article.heading) for anchor in markers.heading_organs):
        return True
    anchors = (*markers.text_organs, *(issuer.anchors if issuer is not None else ()))
    return any(mentions(anchor, article.text) for anchor in anchors)


def authority_fragments(text: str, issuer: OrganStatute | None) -> tuple[str, int, int]:
    """(текст отобранных статей, отобрано, всего статей); статей нет — ``("", 0, 0)``."""
    articles = split_articles(text)
    chosen = [a for a in articles if is_authority_article(a, issuer)]
    return "\n\n".join(a.text for a in chosen), len(chosen), len(articles)


def _fragment_view(reading: SourceReading, issuer: OrganStatute | None) -> SourceView | None:
    "Ужатый вид источника; ``None`` — ужать нечем (у акта нет статей)."
    if reading.raw is None:
        return None
    text, selected, total = authority_fragments(str(reading.raw.full_text), issuer)
    if not total:
        return None
    if not selected:
        return SourceView(reading=reading, mode=SourceMode.WITHHELD, text="", total=total)
    notes = [f"ПЕРЕДАНЫ ФРАГМЕНТЫ О ПОЛНОМОЧИЯХ, НЕ ПОЛНЫЙ ТЕКСТ: статей {selected} из {total}"]
    if reading.status is ReadingStatus.TRUNCATED:
        notes.append(f"ТЕКСТ АКТА НЕПОЛНЫЙ: абзацы 1–{reading.fetched_paragraphs} из {reading.total_paragraphs}")
    return SourceView(
        reading=reading, mode=SourceMode.FRAGMENTS, text="\n".join(notes) + "\n\n" + text, selected=selected, total=total
    )


def _total(project_chars: int, views: Sequence[SourceView]) -> int:
    return project_chars + sum(len(v.text) for v in views)


def select_source_texts(
    readings: Sequence[SourceReading],
    *,
    project_chars: int,
    budget_chars: int,
    issuer: OrganStatute | None,
) -> list[SourceView]:
    """C3 целиком: вид каждого источника с текстом, в порядке приоритета ``readings``."""
    views = [SourceView(reading=r, mode=SourceMode.FULL, text=r.model_text()) for r in readings if r.has_text]
    if _total(project_chars, views) <= budget_chars:
        return views
    # 1) Ужатие: от самого большого источника к меньшему, пока не поместится.
    for index in sorted(range(len(views)), key=lambda i: len(views[i].text), reverse=True):
        if _total(project_chars, views) <= budget_chars:
            break
        fragmented = _fragment_view(views[index].reading, issuer)
        if fragmented is not None and len(fragmented.text) < len(views[index].text):
            views[index] = fragmented

    for index in sorted(range(len(views)), key=lambda i: len(views[i].text), reverse=True):
        if _total(project_chars, views) <= budget_chars:
            break
        view = views[index]
        if view.reading.ref.role is SourceRole.STATUTE or view.mode is SourceMode.WITHHELD:
            continue
        views[index] = SourceView(reading=view.reading, mode=SourceMode.WITHHELD, text="")
    return views


def review_limitations(refs: CompetenceRefs, sources: CompetenceSources, views: Sequence[SourceView]) -> list[str]:
    """Собрать ограничения чтения, отбора фрагментов и установления реквизитов проекта."""
    limitations = list(sources.limitations)
    for view in views:
        limitation = view.limitation()
        if limitation:
            limitations.append(limitation)
    if not refs.issuer:
        limitations.append("Орган-издатель проекта не установлен при разборе шапки.")
    if not refs.region:
        limitations.append("Регион проекта не установлен при разборе шапки.")
    return limitations


def source_block(view: SourceView, text: str) -> str:
    reading = view.reading
    head = f"source={reading.source_id} — {reading.ref.label}"
    if view.mode is SourceMode.FRAGMENTS:
        head += " (переданы фрагменты)"
    return f"{head}\n{text}"


def _walk(node: DocumentStructureNode) -> Iterator[DocumentStructureNode]:
    yield node
    for child in node.children:
        yield from _walk(child)


def split_articles(text: str) -> list[Article]:
    "Статьи акта целиком: от заголовка статьи до следующей статьи, главы, раздела, приложения."
    tree = build_document_structure_tree(text)
    bounds = sorted((n for n in _walk(tree.root) if n.kind in _BOUNDARY_KINDS), key=lambda n: n.char_start)
    articles: list[Article] = []
    for index, node in enumerate(bounds):
        if node.kind != "article":
            continue
        end = bounds[index + 1].char_start if index + 1 < len(bounds) else len(text)
        body = text[node.char_start : end].rstrip()
        articles.append(Article(heading=body.split("\n", 1)[0].strip(), text=body))
    return articles
