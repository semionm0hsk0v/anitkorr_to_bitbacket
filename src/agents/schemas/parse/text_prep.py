from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TextSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    char_start: int
    char_end: int
    text: str
