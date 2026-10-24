"""质保兑换成员免小时级/额度清退，只在当前 Team 订阅到期后下线。"""
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from app.models import RedemptionCode, RedemptionRecord, Team
from app.utils.time_utils import get_now


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


def protect_mapping(mapping, team=None):
    deadline = (team.expires_at if team and mapping.status == "joined"
                and mapping.member_role != "account-owner" else None)
    changed = mapping.auto_kick_at != deadline or mapping.departure_policy != "team_expiry"
    mapping.departure_policy = "team_expiry"
    mapping.auto_kick_enabled = False
    mapping.auto_kick_exempt = True
    mapping.auto_kick_at = deadline
    if changed:
        mapping.auto_kick_failures = 0
        mapping.auto_kick_last_error = None
        mapping.auto_kick_retry_at = None


async def scheduled_departure_allowed(db, team_id, email):
    if not await is_warranty_member(db, team_id, email):
        return True
    # 执行前从数据库重读，避免 Team 续费后仍执行旧的到期任务。
    expiry = await db.scalar(select(Team.expires_at).where(Team.id == team_id))
    return bool(expiry and expiry <= get_now())


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
    team = await db.get(Team, team_id)
    for mapping in mappings:
        if mapping.email.strip().lower() in normalized:
            protect_mapping(mapping, team)
            mapping.join_source = "redeem"
