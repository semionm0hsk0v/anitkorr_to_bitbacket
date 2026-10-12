from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Article(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    heading: str
    text: str
