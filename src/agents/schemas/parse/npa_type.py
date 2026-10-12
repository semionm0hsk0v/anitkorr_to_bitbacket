from __future__ import annotations

from pydantic import BaseModel, Field

from src.agents.enums.parse.npa_type import NpaTypeDetection


class NpaType(BaseModel):
    matched_types: list[str] = Field(default_factory=list)
    out_of_scope: bool = False

    detection: NpaTypeDetection = NpaTypeDetection.DISABLED
