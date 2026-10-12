import pytest

from src.agents.schemas.competence.parse import CitedAct, CompetenceParse
from src.agents.steps_tools.authority.requisites import project_title_from_header, refs_from_header, refs_from_model


@pytest.mark.parametrize(
    "quote, number, date",
    [
        ("от 02.11.2020 № 127-ЗО", "127-ЗО", "02.11.2020"),
        ("от 2 ноября 2020 года № 127-ЗО", "127-ЗО", "02.11.2020"),
        ("от 4 февраля 2013 № 07", "07", "04.02.2013"),
        ("от 16.11.2018 № 25/564-П", "25/564-П", "16.11.2018"),
        ("от 15.05.1991 № 1244-I", "1244-I", "15.05.1991"),
        ("№ 14", "14", ""),
    ],
)
def test_requisites_come_from_exact_citation(quote, number, date):
    parsed = CompetenceParse(
        issuer="",
        region="",
        form="приказ",
        action="amend",
        target_act=CitedAct(kind="regional_order", quote=quote),
        cited_acts=[],
    )
    refs, dropped = refs_from_model(parsed, "Изменить приказ " + quote)
    assert dropped == 0
    assert refs.target_act is not None
    assert (refs.target_act.number, refs.target_act.date) == (number, date)


@pytest.mark.parametrize("quote", ["от 01.01.2020 № 99", "№ 1 и № 2", "акт без номера"])
def test_invented_or_ambiguous_reference_is_dropped(quote):
    parsed = CompetenceParse(
        issuer="",
        region="",
        form="приказ",
        action="approve",
        target_act=None,
        cited_acts=[CitedAct(kind="regional_order", quote=quote)],
    )
    refs, dropped = refs_from_model(parsed, "Акты: № 1 и № 2; акт без номера.")
    assert dropped == 1
    assert refs.cited_acts == ()


def test_unnumbered_code_keeps_exact_name():
    quote = "Гражданский кодекс Российской Федерации"
    parsed = CompetenceParse(
        issuer="",
        region="",
        form="приказ",
        action="approve",
        target_act=None,
        cited_acts=[CitedAct(kind="code", quote=quote)],
    )
    refs, dropped = refs_from_model(parsed, "В соответствии с " + quote)
    assert dropped == 0
    assert refs.cited_acts[0].raw == quote
    assert refs.cited_acts[0].number == refs.cited_acts[0].date == ""


def test_regional_resolution_title_comes_from_source():
    quote = "от 16.11.2018 № 25/564-П"
    parsed = CompetenceParse(
        issuer="",
        region="",
        form="приказ",
        action="approve",
        target_act=None,
        cited_acts=[CitedAct(kind="regional_government_resolution", quote=quote)],
    )
    refs, dropped = refs_from_model(parsed, f"постановлением Правительства области {quote} «О Министерстве»")
    assert dropped == 0
    assert refs.cited_acts[0].title == "О Министерстве"


def test_explicit_project_title_preserves_words_and_ignores_cited_title():
    header = (
        "Министерство\nПРИКАЗ\nОб утверждении\nпорядка выплат\n"
        "В соответствии с законом от 01.01.2020 № 1 «О другой теме» приказываю:"
    )
    assert project_title_from_header(header) == "Об утверждении порядка выплат"


@pytest.mark.parametrize(
    "header",
    [
        "Об утверждении порядка\nВ соответствии с законом",  # нет отдельной строки формы
        "ПРИКАЗ\nОб утверждении порядка\nприказываю:",  # нет отдельной строки преамбулы
        "ПРИКАЗ\nОб утверждении порядка\nО другой теме\nВ соответствии с законом",  # неоднозначно
    ],
)
def test_unrecognized_title_layout_stays_with_model(header):
    assert project_title_from_header(header) == ""


HEADER = (
    "Проект\nМинистерство социального развития Ульяновской области\nПРИКАЗ\n"
    "Об утверждении порядка\nВ соответствии с Федеральным законом от 12.01.1995 № 5-ФЗ приказываю:"
)


def test_standard_header_preserves_metadata_and_references_without_generation():
    refs = refs_from_header(HEADER)
    assert refs is not None
    assert refs.issuer == "Министерство социального развития Ульяновской области"
    assert refs.region == "Ульяновской области"
    assert refs.form == "приказ" and refs.action == "approve"
    assert refs.project_title == "Об утверждении порядка"
    assert refs.target_act is None
    assert len(refs.cited_acts) == 1
    assert (refs.cited_acts[0].kind, refs.cited_acts[0].number, refs.cited_acts[0].date) == (
        "federal_law",
        "5-ФЗ",
        "12.01.1995",
    )


@pytest.mark.parametrize(
    "header",
    [
        HEADER.replace("социального развития", "неизвестного направления"),
        HEADER.replace("Об утверждении порядка", "О признании приказов утратившими силу"),
        HEADER.replace("Федеральным законом от 12.01.1995 № 5-ФЗ", "приказом от 01.01.2020 № 1 и от 02.01.2020 № 2"),
        HEADER.replace("от 12.01.1995 ", ""),
        HEADER.replace("Федеральным законом от 12.01.1995 № 5-ФЗ", "5-ФЗ"),
        HEADER.replace("Федеральным законом от 12.01.1995 № 5-ФЗ", "Трудовым кодексом Российской Федерации"),
    ],
)
def test_partial_or_unsupported_header_uses_model(header):
    assert refs_from_header(header) is None


def test_amendment_keeps_target_separate_from_preamble():
    header = HEADER.replace("Об утверждении порядка", "О внесении изменений в приказ от 01.02.2020 № 7")
    refs = refs_from_header(header)
    assert refs is not None and refs.action == "amend"
    assert refs.target_act is not None and refs.target_act.number == "7"
    assert [ref.number for ref in refs.cited_acts] == ["5-ФЗ"]


def test_standard_header_accepts_indented_document_lines():
    header = "\n".join("  " + line for line in HEADER.splitlines())
    refs = refs_from_header(header)
    assert refs is not None and refs.project_title == "Об утверждении порядка"
