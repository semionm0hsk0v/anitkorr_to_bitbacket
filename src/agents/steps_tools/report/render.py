"""Оформление готового текста заключения в байты DOCX."""

from collections.abc import Sequence
from io import BytesIO

from docx import Document
from docx.shared import Cm, Pt

from src.common.config.config import settings


def report_docx(body: Sequence[str], summary: Sequence[str], *, source_name: str, analysis_date: str) -> bytes:
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Cm(float(settings["agents.report.docx.vertical_margin_cm"]))
    section.left_margin = Cm(float(settings["agents.report.docx.left_margin_cm"]))
    section.right_margin = Cm(float(settings["agents.report.docx.right_margin_cm"]))
    normal = document.styles["Normal"]
    normal.font.name = settings["agents.report.docx.font_name"]
    normal.font.size = Pt(float(settings["agents.report.docx.font_size_pt"]))
    document.add_heading(settings["agents.report.docx.heading"], 0)
    document.add_paragraph(source_name)
    document.add_paragraph(settings["agents.report.docx.analysis_date"].format(analysis_date=analysis_date))
    for paragraph in [*body, *summary]:
        document.add_paragraph(paragraph)
    output = BytesIO()
    document.save(output)
    return output.getvalue()
