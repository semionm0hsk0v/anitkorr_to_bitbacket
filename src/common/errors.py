from abc import ABC
from typing import ClassVar

from starlette import status


class MyBaseError(Exception, ABC):
    cause: ClassVar[str | None] = None
    code_status: ClassVar[int | None] = None


class AntikorrError(MyBaseError):
    cause = "Ошибка пайплайна антикоррупционной экспертизы"
    code_status = status.HTTP_500_INTERNAL_SERVER_ERROR


class DocumentParseError(AntikorrError):
    cause = "Ошибка парсинга входящего документа"
    code_status = status.HTTP_422_UNPROCESSABLE_ENTITY


class IntakeError(AntikorrError):
    "Ошибка состава загрузки: её текст показывается пользователю дословно."

    cause = "Некорректный состав загрузки"
    code_status = status.HTTP_400_BAD_REQUEST
