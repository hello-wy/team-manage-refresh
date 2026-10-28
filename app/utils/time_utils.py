from datetime import datetime
import pytz
from app.config import settings

def get_now() -> datetime:
    """获取当前时区的当前时间 (返回 naive datetime 以保持数据库兼容性)"""
    tz = pytz.timezone(settings.timezone)
    return datetime.now(tz).replace(tzinfo=None)


def to_local_iso(dt: datetime) -> str:
    """为数据库中的本地时间补充明确时区，供浏览器进行时间计算。"""
    tz = pytz.timezone(settings.timezone)
    localized = tz.localize(dt) if dt.tzinfo is None else dt.astimezone(tz)
    return localized.isoformat()
