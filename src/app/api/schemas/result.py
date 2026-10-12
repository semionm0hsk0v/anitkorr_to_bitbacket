import traceback as traceback_module

from pydantic import BaseModel
from starlette.requests import Request

from src.common import MyBaseError
from src.common.config.config import settings


class ErrorModel(BaseModel):
    exception: str  # Имя класса
    cause: str  # Общее описание причины ошибки
    details: str  # Конкретные данные послужившие возникновению ошибки
    request_url: str  # Какой URL пришел в запросе
    request_method: str  # POST/GET etc
    request_headers: list[str]  # Список заголовков запроса
    traceback: list[str]  # Путь в коде до ошибки, в режиме PROD=[] для безопасности, но в логе есть

    @staticmethod
    def make(err: Exception, req: Request) -> "ErrorModel":
        match err:
            case MyBaseError() as se:
                cause = se.cause or "Причина ошибки неизвестна"
            case _:
                cause = "Причина ошибки неизвестна"

        return ErrorModel(
            exception=err.__class__.__name__,
            cause=cause,
            details=str(err),
            request_url=str(req.url),
            request_method=str(req.method),
            request_headers=[f"{k}: {v}" for k, v in req.headers.items()],
            traceback=list(traceback_module.format_list(traceback_module.extract_tb(err.__traceback__)))
            if settings["API_MODE"] == "DEV"
            else [],
        )
