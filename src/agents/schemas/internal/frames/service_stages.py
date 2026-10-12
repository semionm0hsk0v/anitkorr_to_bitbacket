from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict


class ServiceStage(BaseModel):
    """Этап: значение области срока и то, как он узнаётся в тексте."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    #: Корень в тексте проекта (регистр не снят; ``ё`` и ``е`` различаются шаблоном).
    search: re.Pattern[str]
    #: Словоформы существительного, называющего этап (для головы строки срока).
    noun: re.Pattern[str]

    family: tuple[str, re.Pattern[str]] | None

    nested: bool
