"""质保码入组成员的自动清退保护，不受质保到期时间影响。"""
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from app.models import RedemptionCode, RedemptionRecord
from app.services.member_auto_kick_policy import set_participation


def warranty_member_condition(team_id, email):
    """同时兼容兑换记录和旧版本仅保留的兑换码使用绑定。"""
    code = aliased(RedemptionCode)
    normalized_email = func.lower(func.trim(email))
    recorded = (select(RedemptionRecord.id)
                .outerjoin(code, code.code == RedemptionRecord.code)
                .where(RedemptionRecord.team_id == team_id,
                       func.lower(func.trim(RedemptionRecord.email)) == normalized_email,
                       or_(RedemptionRecord.is_warranty_redemption.is_(True),
                           code.has_warranty.is_(True)))
                .correlate_except(RedemptionRecord, code).exists())
    used = (select(code.id)
            .where(code.has_warranty.is_(True), code.used_team_id == team_id,
                   func.lower(func.trim(code.used_by_email)) == normalized_email)
            .correlate_except(code).exists())
    return or_(recorded, used)


async def is_warranty_member(db, team_id, email):
    if not email:
        return False
    return bool(await db.scalar(select(warranty_member_condition(team_id, email))))


def protect_mapping(mapping):
    set_participation(mapping, False, 0)
    mapping.auto_kick_failures = 0
    mapping.auto_kick_last_error = None
    mapping.auto_kick_retry_at = None


async def protect_team_mappings(db, team_id, mappings):
    """一次查询保护整组成员，兼容首次同步、历史成员和重新入组。"""
    if not mappings:
        return
    # 直接查兑换历史，首次同步的新 mapping 尚未落库也能匹配。
    recorded = select(RedemptionRecord.email).outerjoin(
        RedemptionCode, RedemptionCode.code == RedemptionRecord.code,
    ).where(RedemptionRecord.team_id == team_id,
            or_(RedemptionRecord.is_warranty_redemption.is_(True),
                RedemptionCode.has_warranty.is_(True)))
    used = select(RedemptionCode.used_by_email).where(
        RedemptionCode.used_team_id == team_id, RedemptionCode.has_warranty.is_(True),
    )
    emails = (await db.scalars(recorded.union(used))).all()
    normalized = {email.strip().lower() for email in emails if email}
    for mapping in mappings:
        if mapping.email.strip().lower() in normalized:
            protect_mapping(mapping)
