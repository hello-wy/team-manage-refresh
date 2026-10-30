"""按工作台账号读取到期时间，仅在没有唯一可用时间时使用明确设置的时间。"""
from collections import defaultdict
from datetime import datetime

import pytz
from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.config import settings
from app.models import Team
from app.official_key_model import OfficialKey, OfficialKeyExpiration
from app.utils.time_utils import to_local_iso


def _expiration(*, dates, manual):
    unique_dates = set(dates)
    if len(unique_dates) == 1:
        return {"expires_at": unique_dates.pop(), "source": "team", "can_edit": False,
                "note": "来自工作台同账号 Team"}
    note = "同账号存在多个不同的 Team 到期时间，请手动设置" if unique_dates else "未获取到 Team 到期时间，可手动设置"
    return {"expires_at": manual, "source": "manual" if manual else None,
            "can_edit": True, "note": note}


async def read_expirations(db, *, entries):
    accounts = {entry["account"].strip().lower() for entry in entries}
    if not accounts:
        return {}
    teams = (await db.execute(select(Team.email, Team.expires_at).where(
        func.lower(func.trim(Team.email)).in_(accounts), Team.expires_at.is_not(None)
    ))).all()
    dates = defaultdict(list)
    for email, expires_at in teams:
        dates[email.strip().lower()].append(expires_at)
    manual = dict((await db.execute(select(
        OfficialKeyExpiration.official_key_id, OfficialKeyExpiration.expires_at
    ).where(OfficialKeyExpiration.official_key_id.in_([entry["id"] for entry in entries])))).all())
    return {entry["id"]: _expiration(
        dates=dates[entry["account"].strip().lower()], manual=manual.get(entry["id"])
    ) for entry in entries}


def expiration_views(expirations, *, render_time):
    return {entry_id: {
        **view,
        "expires_at": to_local_iso(view["expires_at"]) if view["expires_at"] else None,
        "input_value": view["expires_at"].strftime("%Y-%m-%dT%H:%M") if view["expires_at"] else "",
        "html": str(render_time(view["expires_at"])),
    } for entry_id, view in expirations.items()}


def _local_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(pytz.timezone(settings.timezone)).replace(tzinfo=None)


async def save_manual_expiration(db, *, entry_id, expires_at):
    entry = await db.get(OfficialKey, entry_id)
    if entry is None:
        raise HTTPException(404, "官key 记录不存在")
    current = await read_expirations(db, entries=[{"id": entry.id, "account": entry.account}])
    if not current[entry_id]["can_edit"]:
        raise HTTPException(409, "已获取到工作台 Team 到期时间，请在工作台更新")
    if expires_at is None:
        await db.execute(delete(OfficialKeyExpiration).where(
            OfficialKeyExpiration.official_key_id == entry_id
        ))
    else:
        await db.merge(OfficialKeyExpiration(
            official_key_id=entry_id, expires_at=_local_datetime(expires_at)
        ))
    await db.commit()
