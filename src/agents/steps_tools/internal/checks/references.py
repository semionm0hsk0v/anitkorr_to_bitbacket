from __future__ import annotations

import re

from src.agents.enums.internal.detector import InternalCheck
from src.agents.schemas.internal.addressing.address_space import Address, SubjectMismatch
from src.agents.schemas.internal.findings import DetectorFinding
from src.agents.schemas.parse.references import InternalRef
from src.agents.schemas.parse.structure import DocumentStructureNode, DocumentStructureTree
from src.agents.steps_tools.document.locus_labels import humanize_locus
from src.agents.steps_tools.document.norm_fragments import sentence_spans
from src.agents.steps_tools.document.references import scan_internal_refs
from src.agents.steps_tools.document.segmentation import UNLABELED_PROVISION
from src.agents.steps_tools.internal.addressing.address_space import build_address_book
from src.agents.steps_tools.internal.addressing.resolution import check_subject, resolve_reference
from src.agents.steps_tools.internal.findings import detector_finding
from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)

#: Метка узла-приложения в дереве («прил. 1, абз. 1»).
_APPENDIX_LOCUS = re.compile(r"прил\.\s*\d", re.IGNORECASE)


def _iter_leaves(node: DocumentStructureNode):
    if not node.children:
        if (node.text or "").strip():
            yield node
        return
    for ch in node.children:
        yield from _iter_leaves(ch)


_QUOTE_PAD = int(settings["agents.internal.references_check.quote_pad"])


def _reference_quote(text: str, lo: int, hi: int, sentences: list[tuple[int, int]]) -> str:
    "Цитата ОДНОЙ отсылки: её ссылочная фраза ``[lo, hi)`` и до ``_QUOTE_PAD`` знаков вокруг."
    sentence_start, sentence_end = next(((a, b) for a, b in sentences if a < hi <= b), (0, len(text)))
    start = max(sentence_start, lo - _QUOTE_PAD)
    end = min(sentence_end, hi + _QUOTE_PAD)
    while start > sentence_start and text[start - 1].isalnum() and text[start].isalnum():
        start -= 1
    while end < sentence_end and text[end - 1].isalnum() and text[end].isalnum():
        end += 1
    return text[start:end].strip()


def _dead_ref_rationale(addresses: list[Address], phrases: str) -> str:
    """Формулировка для мёртвой отсылки — с человекочитаемым адресом-адресатом."""
    human = ", ".join(a.human() for a in addresses)
    return f"Отсылка ({phrases}) ведёт к отсутствующему положению: {human} в проекте не найден(ы)."


def _mismatch_rationale(mismatch: SubjectMismatch) -> str:
    """Формулировка для подмены предмета отсылки (адресат есть, но он о другом)."""
    excerpt = " ".join(mismatch.target_text.split())[:120]
    return (
        f"Отсылка сделана применительно к «{mismatch.subject}», но {mismatch.target.human()} "
        f"содержит другое положение («{excerpt}…»); «{mismatch.subject}» — это {mismatch.correct.human()}."
    )


def _in_appendix(label: str) -> bool:
    """Локус лежит в приложении (сводные таблицы дублируют нормы основного текста)."""
    return bool(_APPENDIX_LOCUS.search(label or ""))


def _ref_context(text: str, ref: InternalRef) -> str:
    """Текст перед отсылкой — в нём назван ПРЕДМЕТ («…фотографии, не соответствующей…»)."""
    return text[max(0, ref.char_start - 200) : ref.char_start]


#: Сколько примеров непроверенных отсылок попадает в строку журнала.
_NOT_JUDGED_EXAMPLES = int(settings["agents.internal.references_check.not_judged_examples"])


def _not_judged(leaf: DocumentStructureNode, *, phrase: str, reason: str) -> dict[str, str]:
    """Отсылка, о которой не судили, — запись для журнала, а не молчание: пустой список находок их не отличает."""
    return {"locus": humanize_locus(leaf.label or "") or UNLABELED_PROVISION, "phrase": phrase, "reason": reason}


def reference_findings(tree: DocumentStructureTree, *, table_geometry: dict | None = None) -> list[DetectorFinding]:
    "Ошибочные отсылки проекта к собственным положениям."
    document_text = tree.root.text or ""
    book = build_address_book(document_text, tree, table_geometry=table_geometry)
    findings: list[DetectorFinding] = []
    not_judged: list[dict[str, str]] = []
    #: ключ дефекта подмены предмета → был ли он уже заявлен из ОСНОВНОГО текста
    mismatch_in_core: set[tuple[str, str, str | None, str | None]] = set()
    deferred: list[tuple[tuple[str, str, str | None, str | None], DetectorFinding]] = []

    for leaf in _iter_leaves(tree.root):
        text = leaf.text or ""
        place = humanize_locus(humanize_locus(leaf.label or "") or UNLABELED_PROVISION)
        dead: list[tuple[InternalRef, Address]] = []
        scan = scan_internal_refs(text)
        sentences = sentence_spans(text) if scan.refs else []
        for chain in scan.unresolved:
            not_judged.append(_not_judged(leaf, phrase=chain.phrase, reason=f"{chain.reason_code.value}: {chain.reason}"))
        for ref in scan.refs:
            resolution = resolve_reference(
                book,
                ref,
                source_label=leaf.label or "",
                source_pos=leaf.char_start + ref.char_start,
            )
            if resolution.status == "missing":
                dead.append((ref, resolution.address))
                continue
            if resolution.status != "exists":
                not_judged.append(_not_judged(leaf, phrase=ref.phrase, reason=resolution.reason or resolution.status))
                continue
            mismatch = check_subject(resolution.region, resolution.address, _ref_context(text, ref))
            if mismatch is None:
                continue
            key = (mismatch.stem, mismatch.target.item, mismatch.target.table, mismatch.target.appendix)
            finding = detector_finding(
                InternalCheck.REFERENCE,
                place=place,
                quote=_reference_quote(text, ref.char_start, ref.char_start + len(ref.phrase), sentences),
                rationale=_mismatch_rationale(mismatch),
                seconds=[(mismatch.target.human(), mismatch.target_text)],
                related_places=[mismatch.target.item],
            )
            if _in_appendix(leaf.label or ""):
                deferred.append((key, finding))
            else:
                mismatch_in_core.add(key)
                findings.append(finding)

        if not dead:
            continue
        addresses = sorted({a for _, a in dead}, key=lambda a: a.item)
        phrases = ", ".join(sorted({r.phrase for r, _ in dead}))

        quotes = list(
            dict.fromkeys(
                _reference_quote(text, ref.char_start, ref.char_start + len(ref.phrase), sentences)
                for ref, _ in sorted(dead, key=lambda item: item[0].char_start)
            )
        )
        findings.append(
            detector_finding(
                InternalCheck.REFERENCE,
                place=place,
                quote=quotes[0],
                rationale=_dead_ref_rationale(addresses, phrases),
                seconds=[(place, quote) for quote in quotes[1:]],
                related_places=sorted({a.item for a in addresses}),
            )
        )

    findings.extend(finding for key, finding in deferred if key not in mismatch_in_core)
    if not_judged:
        log.info("internal_references_not_judged", count=len(not_judged), examples=not_judged[:_NOT_JUDGED_EXAMPLES])
    return findings
