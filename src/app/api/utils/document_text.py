import hashlib
import re
from pathlib import Path
from uuid import UUID

from src.agents.steps_tools.document.text_prep import clean_text
from src.common import IntakeError
from src.common.config.config import settings
from src.infrastructure.s3_services.async_client import (
    S3ServiceAsyncClient,
    client as s3_client,
)
from src.infrastructure.s3_services.models import FileMetadata

SUPPORTED_DOC_EXTENSIONS = frozenset({".docx", ".pdf", ".txt"})
MAX_UPLOAD_BYTES: int = int(settings.app.max_upload_mb) * 1024 * 1024

_BR_MARKUP_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


_MD_SEPARATOR_RE = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+$")


def _markdown_row_as_line(row: str) -> str:
    "Строку таблицы — в ОДНУ текстовую строку «9. <ячейки через пробел>»."
    cells = (_BR_MARKUP_RE.sub(" ", cell).strip() for cell in row.strip().strip("|").split("|"))
    return " ".join(cell for cell in cells if cell)


def _normalize_service_markup(text: str) -> str:
    """Разметку сервиса — к той же форме, что даёт текстовый слой PDF."""
    lines: list[str] = []
    header_rows: set[str] = set()
    previous_row = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("|"):
            lines.append(_BR_MARKUP_RE.sub("\n", raw_line.rstrip()))
            previous_row = ""
            continue
        if _MD_SEPARATOR_RE.match(stripped):
            if previous_row:
                header_rows.add(previous_row)
            continue
        if stripped in header_rows:
            continue
        previous_row = stripped
        row = _markdown_row_as_line(stripped)
        if row:
            lines.append(row)
    return "\n".join(lines)


def _decode_processed_text(data: bytes) -> str:
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


async def extract_processed_text_from_s3(file_id: str, client: S3ServiceAsyncClient) -> str:
    "Текст извлечения s3-service, приведённый к тому же канону, что и локальный вход."
    data = await client.download_to_bytes(key=file_id)
    if not data:
        return ""
    if len(data) > MAX_UPLOAD_BYTES:
        raise IntakeError(
            f"Извлечённый текст больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ. Разделите документ и загрузите заново."
        )
    return clean_text(_normalize_service_markup(_decode_processed_text(data)))


def _validate_s3_metadata(metadata: FileMetadata) -> str:
    ext = Path(metadata.original_filename).suffix.lower()
    if ext not in SUPPORTED_DOC_EXTENSIONS:
        raise IntakeError("Неподдерживаемый формат вложения. Загрузите проект в .docx, .pdf или .txt с текстовым слоем.")
    if metadata.size_bytes > MAX_UPLOAD_BYTES:
        raise IntakeError(f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ. Уменьшите размер и загрузите заново.")
    return ext


async def _load_processed_document_from_s3(
    file_id: UUID,
    client: S3ServiceAsyncClient,
) -> tuple[FileMetadata, str, str]:
    """Resolve metadata once and retain the existing canonical-text contract."""
    metadata = await client.metadata(key=str(file_id))
    ext = _validate_s3_metadata(metadata)
    text = await extract_processed_text_from_s3(metadata.file_id, client)
    if not text.strip():
        raise IntakeError(
            "В обработанном документе нет текста. Загрузите проект с выделяемым и копируемым текстом. "
            "Контур не выполняет OCR: для скана сначала распознайте текст вне навыка и загрузите результат заново."
        )
    return metadata, ext, text


def _failed_geometry_bundle(canonical_text: str, reason: str) -> dict:
    """Content-free diagnostic preserving that geometry was not established."""
    return {
        "canonical_text_sha256": hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
        "canonical_text_length": len(canonical_text),
        "source_pdf_sha256": None,
        "rows": [],
        "truncated": True,
        "reason_codes": [reason],
    }


async def extract_document_with_geometry_from_s3(
    file_id: UUID,
    thread_id: str | None = None,
    *,
    client: S3ServiceAsyncClient | None = None,
) -> tuple[str, str, int, dict | None]:
    "Load anonymized text only; no raw PDF may bypass the platform privacy boundary."
    del thread_id  # приёмный слой передаёт его, но загрузка по ``file_id`` от треда не зависит
    active_client = client or s3_client
    metadata, ext, text = await _load_processed_document_from_s3(file_id, active_client)
    base_result = (metadata.original_filename, text, len(text))
    if ext != ".pdf":
        return (*base_result, None)

    return (*base_result, _failed_geometry_bundle(text, "geometry_not_available_from_anonymized_text"))
