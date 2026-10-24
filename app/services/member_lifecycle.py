"""确认入组后激活服务，兑换时间只用于审计与邀请排序。"""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from app.models import RedemptionCode, RedemptionRecord
from app.services.settings import settings_service, WARRANTY_EXPIRATION_MODE_REFRESH_ON_REDEEM
from app.utils.time_utils import get_now


def record_service_start(record):
    if record.activation_pending:
        return None
    return record.service_started_at or record.redeemed_at


def service_start(code, records, mode, reference=None):
    if code.reusable_by_seat and reference is not None:
        return record_service_start(reference)
    if not records:
        return code.used_at
    if mode == WARRANTY_EXPIRATION_MODE_REFRESH_ON_REDEEM:
        latest = max(records, key=lambda row: (row.redeemed_at, row.id or 0))
        if latest.activation_pending:
            return None
        return latest.service_started_at or code.used_at or latest.redeemed_at
    starts = [start for row in records if (start := record_service_start(row))]
    return min(starts) if starts else None


async def activate_joined_redemptions(db, team_id, mappings):
    joined = {m.email: m for m in mappings if m.status == "joined" and m.joined_at}
    if not joined:
        return
    records = (await db.scalars(select(RedemptionRecord).where(
        RedemptionRecord.team_id == team_id, RedemptionRecord.activation_pending.is_(True),
    ))).all()
    changed_codes = set()
    for record in records:
        mapping = joined.get(record.email.strip().lower())
        if mapping is None:
            continue
        # 不使用邀请发送时间起算，也不允许新兑换继承历史入组时间。
        record.service_started_at = max(mapping.joined_at, record.redeemed_at)
        record.activation_pending = False
        changed_codes.add(record.code)
    if not changed_codes:
        return
    await db.flush()
    rows = (await db.execute(select(RedemptionRecord, RedemptionCode).join(
        RedemptionCode, RedemptionCode.code == RedemptionRecord.code,
    ).where(RedemptionRecord.code.in_(changed_codes)))).all()
    by_code = defaultdict(list)
    codes = {}
    for record, code in rows:
        by_code[code.code].append(record)
        codes[code.code] = code
    mode = await settings_service.get_warranty_expiration_mode(db)
    for value, code in codes.items():
        if not code.has_warranty or code.reusable_by_seat:
            continue
        start = service_start(code, by_code[value], mode)
        if start and (not code.warranty_expires_at or mode == WARRANTY_EXPIRATION_MODE_REFRESH_ON_REDEEM):
            days = (code.warranty_days or 30) + max(code.extension_days or 0, 0)
            code.warranty_expires_at = start + timedelta(days=days)
            code.extension_days = 0
        if code.warranty_expires_at:
            code.status = "warranty_active" if code.warranty_expires_at >= get_now() else "expired"
