from datetime import datetime

from dateutil import tz


def datetime_msk(utc: datetime | None = None) -> datetime:
    utc = utc or datetime.utcnow()
    from_zone = tz.tzutc()
    to_zone = tz.gettz("Europe/Moscow")

    utc = utc.replace(tzinfo=from_zone)
    msk = utc.astimezone(to_zone)

    return msk
