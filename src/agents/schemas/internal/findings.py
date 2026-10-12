from pydantic import BaseModel, ConfigDict, Field

from src.agents.enums.internal.detector import InternalCheck
from src.agents.schemas.common.review import Risk


class DetectorFinding(BaseModel):
    """Находка одной проверки детектора: готовый ``Risk`` и то, что к нему не относится.

    ``risk`` уходит в заключение. ``check`` и ``related_places`` в ``Risk`` не живут: первое — какая проверка
    нашла, второе — места второй стороны так, как их называла проверка (для отсылок это адреса адресатов,
    которых в проекте может не быть). Ими пользуются совпадение с находками эксперта, логи и стенд сравнения.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    check: InternalCheck
    risk: Risk
    related_places: tuple[str, ...] = Field(default=(), description="Места второй стороны: прежнее cross_refs находки")
