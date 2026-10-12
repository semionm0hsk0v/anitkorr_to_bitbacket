"""Регрессии координат и предметных значений после разделения steps_tools."""

import pytest

from src.agents.enums.internal.frames.norm_frame import GroundingStatus
from src.agents.enums.parse.segmentation import SegmentAddressStatus
from src.agents.schemas.internal.detector import DetectorInput
from src.agents.schemas.parse.parsed_document import ParsedDocument
from src.agents.steps_tools.document.norm_fragments import iter_normative_fragments
from src.agents.steps_tools.document.segmentation import segment_norms
from src.agents.steps_tools.document.structure import build_document_structure_tree
from src.agents.steps_tools.internal.detector import scan_internal
from src.agents.steps_tools.internal.frames.project_frames import prepare_project_frames

PROJECTS = {
    "plain": "Заявитель предоставляет документ.\n\nОрган рассматривает заявление.",
    "dead_reference": "ПОРЯДОК\n1. Документы предоставляются в соответствии с пунктом 9 настоящего Порядка.",
    "ocr_numbering": "ПОРЯДОК 1. Общие положения 1.1.Заявление регистрируется. 1.2. Решение направляется заявителю.",
    "repeated_text": "ПОРЯДОК\n1. Заявление регистрируется.\n2. Заявление регистрируется.",
    "durations": (
        "ПОРЯДОК\n1. Регистрация заявления осуществляется в течение 3 рабочих дней со дня поступления заявления.\n"
        "2. Решение принимается в течение 5 рабочих дней со дня регистрации заявления."
    ),
    "subservice": "ПОРЯДОК\n1. В части выдачи разрешения: заявление регистрируется в течение 3 рабочих дней.",
    "separate_appendices": (
        "ПРИКАЗЫВАЮ:\n1. Утвердить Порядок.\nПРИЛОЖЕНИЕ № 1\nПОРЯДОК\n"
        "1. Регистрация заявления осуществляется в течение 3 дней.\nПРИЛОЖЕНИЕ № 2\nПОРЯДОК\n"
        "1. Регистрация заявления осуществляется в течение 5 дней."
    ),
    "quoted_amendment": (
        "О внесении изменений в Порядок\nПРИКАЗЫВАЮ:\n1. Пункт 2 изложить в следующей редакции:\n"
        "«2. Заявление подаётся в соответствии с пунктом 9 Порядка»."
    ),
    "tables": (
        "ПОРЯДОК\n1. Документы приведены в таблице 1 приложения № 1.\n"
        "ПРИЛОЖЕНИЕ № 1\nТаблица 1\n1. Паспорт заявителя\n2. Свидетельство о рождении ребёнка"
    ),
    "signature": "Об утверждении Порядка\nПРИКАЗЫВАЮ:\n1. Утвердить Порядок.\nМинистр И.И. Иванов",
}


@pytest.mark.parametrize("text", PROJECTS.values(), ids=PROJECTS)
def test_original_coordinates_and_detector_checks_survive_document_reflow(text):
    segments = segment_norms(text, normalize=False)
    assert segments
    resolved = [segment for segment in segments if segment.address_status is SegmentAddressStatus.RESOLVED]
    assert resolved
    for segment in resolved:
        assert text[segment.char_start : segment.char_end] == segment.text
    tree = build_document_structure_tree(text, segments=segments)
    data = DetectorInput(document_text=text, parsed=ParsedDocument(enriched_text=text, tree=tree))
    scan = scan_internal(data)
    # Ноль находок при скрытом сбое одной из проверок не считается успешной регрессией.
    assert scan.failed == {}


def frames(text):
    tree = build_document_structure_tree(text)
    return prepare_project_frames(iter_normative_fragments(tree.root), document_text=text)


def test_repeated_norms_keep_their_own_source_coordinates():
    text = PROJECTS["repeated_text"]
    repeated = [item for item in frames(text) if "Заявление регистрируется" in item.leaf.text]
    assert len(repeated) == 2
    spans = [item.frame.evidence.primary_span for item in repeated]
    assert all(span is not None and span.grounding_status is GroundingStatus.EXACT for span in spans)
    assert spans[0].char_start < spans[1].char_start
    assert spans[0].span_id != spans[1].span_id


def test_duration_endpoints_remain_bound_to_their_own_norms():
    built = [item for item in frames(PROJECTS["durations"]) if item.parameters]
    assert len(built) == 2
    constraints = [item.frame.regulated_variable.duration_constraints[0] for item in built]
    assert [item.normalized_value for item in constraints] == [3, 5]
    assert [item.start_event.raw_value for item in constraints] == ["поступления заявления", "регистрации заявления"]


def test_proven_subservice_is_a_text_value_with_its_own_evidence():
    built = next(item for item in frames(PROJECTS["subservice"]) if item.parameters)
    slot = built.frame.subservice
    assert slot.raw_value == "выдачи разрешения"
    assert slot.role_is_proven()
    assert slot.evidence_span_ids
