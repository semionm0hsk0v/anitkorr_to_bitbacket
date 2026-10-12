from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from http import HTTPStatus
from urllib.parse import quote

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.common import DocumentParseError
from src.common.config.config import settings

from .models import FileMetadata, UploadResult


class AnonymizingFileIsNotReadyError(Exception):
    pass


class S3ServiceClientError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class S3FileNotFoundError(S3ServiceClientError, DocumentParseError):
    pass


retry_on_not_ready = retry(
    stop=stop_after_attempt(int(settings["S3_SERVICE_RETRY_ON_NOT_READY_ATTEMPTS"])),
    wait=wait_exponential(
        multiplier=float(settings["S3_SERVICE_RETRY_ON_NOT_READY_WAIT_MULTIPLIER"]),
        min=float(settings["S3_SERVICE_RETRY_ON_NOT_READY_WAIT_MIN_SECONDS"]),
        max=float(settings["S3_SERVICE_RETRY_ON_NOT_READY_WAIT_MAX_SECONDS"]),
    ),
    retry=retry_if_exception_type(AnonymizingFileIsNotReadyError),
    reraise=True,
)


class S3ServiceAsyncClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> S3ServiceAsyncClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    def _service_headers(self) -> dict[str, str]:
        if not self._service_token:
            raise S3ServiceClientError("Service token is required for this operation")
        return {"Authorization": f"Bearer {self._service_token}"}

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code == HTTPStatus.NOT_FOUND:
            raise S3FileNotFoundError("File not found")

        if response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
            raise S3ServiceClientError("Не удалось обработать документ.", status_code=response.status_code)
        raise S3ServiceClientError("Сервис документов недоступен.", status_code=response.status_code)

    @staticmethod
    def _encode_key(key: str) -> str:
        return quote(key, safe="/")

    async def metadata(self, *, key: str) -> FileMetadata:
        encoded_key = self._encode_key(key)
        response = await self._client.get(
            f"{settings['S3_SERVICE_META_PATH']}/{encoded_key}",
            headers=self._service_headers(),
        )
        self._raise_for_status(response)
        data = response.json()
        return FileMetadata(
            file_id=data["file_id"],
            bucket_name=data["bucket_name"],
            original_filename=data["original_filename"],
            storage_key=data["storage_key"],
            size_bytes=data["size_bytes"],
            content_type=data.get("content_type"),
            created_by=data.get("created_by"),
        )

    async def exists(self, *, key: str, created_by: str | None = None) -> bool:
        encoded_key = self._encode_key(key)
        path = f"{settings['S3_SERVICE_EXISTS_PATH']}/{encoded_key}"
        params = {"created_by": created_by} if created_by else None
        response = await self._client.get(path, params=params, headers=self._service_headers())
        if response.status_code == HTTPStatus.NOT_FOUND:
            return False
        self._raise_for_status(response)
        return bool(response.json()["exists"])

    async def upload(
        self,
        *,
        key: str,
        file_stream: AsyncIterator[bytes],
        original_filename: str,
        max_bytes: int | None = None,
        created_by: str | None = None,
        chat_id: str | None = None,
        file_id: str | None = None,
        kind: str = "source",
    ) -> UploadResult:
        headers = self._service_headers()
        headers["X-File-Name-B64"] = base64.b64encode(original_filename.encode("utf-8")).decode("ascii")
        safe_ascii_filename = original_filename.encode("ascii", "ignore").decode("ascii").strip()
        headers["X-File-Name"] = safe_ascii_filename or "document"
        if created_by:
            headers["x-created-by"] = created_by

        params: dict[str, str | int] = {"kind": kind}
        if max_bytes is not None:
            params["max_bytes"] = max_bytes
        if chat_id:
            params["chat_id"] = chat_id
        if file_id:
            params["id"] = file_id

        encoded_key = self._encode_key(key)
        upload_path = settings["S3_SERVICE_UPLOAD_PATH"]
        response = await self._client.post(
            f"{upload_path}/{encoded_key}",
            params=params,
            content=file_stream,
            headers=headers,
        )
        self._raise_for_status(response)
        data = response.json()
        return UploadResult(
            status=data["status"],
            file_id=data["file_id"],
            storage_key=data["storage_key"],
        )

    @retry_on_not_ready
    async def download_to_bytes(
        self,
        *,
        key: str,
    ) -> bytes:
        encoded_key = self._encode_key(key)
        response = await self._client.get(
            f"/s3/download/{encoded_key}/anonymized",
            headers=self._service_headers(),
        )
        if response.status_code == HTTPStatus.ACCEPTED:
            raise AnonymizingFileIsNotReadyError(f"File {key} is still processing in s3-service (202 Accepted)")
        self._raise_for_status(response)
        return response.content


client = S3ServiceAsyncClient(
    base_url=str(settings["S3_SERVICE_BASE_URL"]),
    service_token=settings.get("S3_SERVICE_ACCESS_TOKEN"),
    timeout=20.0,
)
