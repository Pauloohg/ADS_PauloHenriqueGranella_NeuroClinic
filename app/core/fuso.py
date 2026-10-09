from datetime import datetime, timezone
from zoneinfo import ZoneInfo

FUSO_CLINICA = ZoneInfo("America/Sao_Paulo")


def utc_para_local(dt_utc: datetime) -> datetime:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(FUSO_CLINICA).replace(tzinfo=None)
