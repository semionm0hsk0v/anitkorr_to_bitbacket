from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agents.enums.parse.segmentation import ProvisionKind, SegmentAddressReason, SegmentAddressStatus


class NormSegment(BaseModel):
    """Один объект экспертизы: норма или положение проекта НПА."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: int
    text: str
    char_start: int
    char_end: int
    source_ref: str
    kind: ProvisionKind
    parent_intro: str | None = None

    is_letter_subclause: bool = False
    #: Адрес доказан переносом происхождения символов, а не поиском подстроки.
    address_status: SegmentAddressStatus = SegmentAddressStatus.RESOLVED
    address_reason: SegmentAddressReason | None = None
