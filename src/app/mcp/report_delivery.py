"""Render the graph's report once and deliver its anonymized DOCX through S3."""

from pathlib import Path
from uuid import UUID, uuid4

from src.agents.schemas.common.review import Review
from src.agents.steps_tools.report.assemble import body_paragraphs
from src.agents.steps_tools.report.render import report_docx
from src.infrastructure.s3_services.async_client import S3ServiceClientError, client as s3_client


async def deliver_report(*, review: Review, summary: str, source_name: str, analysis_date: str, thread_id: str) -> str:
    document = report_docx(body_paragraphs(review), summary.splitlines(), source_name=source_name, analysis_date=analysis_date)
    filename = f"Заключение_{Path(source_name).stem or 'Проект'}.docx"

    async def chunks():
        yield document

    result = await s3_client.upload(
        key=f"antikorr/reports/{uuid4()}/report.docx",
        file_stream=chunks(),
        original_filename=filename,
        chat_id=str(UUID(thread_id)),
        kind="anonymized",
    )
    if result.status != "ok":
        raise S3ServiceClientError("Report upload was not confirmed")
    return str(UUID(result.file_id))
