"""集中构建席位权益与时间预览，前端不推测兑换来源。"""
from datetime import timedelta

from sqlalchemy import or_, select

from app.models import RedemptionCode, RedemptionRecord
from app.services.member_lifecycle import service_start
from app.services.settings import settings_service
from app.utils.time_utils import get_now


def iso(value):
    return value.isoformat() if value else None


async def decorate_seat_status(db, team, members):
    records = (await db.scalars(select(RedemptionRecord).where(
        RedemptionRecord.team_id == team.id,
    ))).all()
    values = {r.code for r in records}
    histories = {}
    codes = {c.code: c for c in (await db.scalars(select(RedemptionCode).where(or_(
        RedemptionCode.code.in_(values),
        (RedemptionCode.used_team_id == team.id) & RedemptionCode.has_warranty.is_(True),
    )))).all()}
    legacy_codes = {(c.used_by_email or '').strip().lower(): c for c in codes.values()
                    if c.has_warranty and c.used_team_id == team.id}
    values.update(codes)
    if values:
        for record in (await db.scalars(select(RedemptionRecord).where(
            RedemptionRecord.code.in_(values),
        ))).all():
            histories.setdefault(record.code, []).append(record)
    mode = await settings_service.get_warranty_expiration_mode(db)
    by_email = {}
    for record in sorted(records, key=lambda r: (r.redeemed_at, r.id)):
        code = codes.get(record.code)
        if record.is_warranty_redemption or (code and code.has_warranty):
            by_email[record.email.strip().lower()] = record
    now = get_now()
    for member in members:
        record = by_email.get((member.get('email') or '').strip().lower())
        code = codes.get(record.code) if record else legacy_codes.get((member.get('email') or '').strip().lower())
        warranty_expiry = None
        if record:
            if code:
                start = service_start(code, histories.get(record.code, []), mode, record)
                warranty_expiry = (code.warranty_expires_at if not code.reusable_by_seat else None)
                if not warranty_expiry and start:
                    warranty_expiry = start + timedelta(days=(code.warranty_days or 30) + (code.extension_days or 0))
        elif code:
            warranty_expiry = code.warranty_expires_at
            start = service_start(code, histories.get(code.code, []), mode)
            if not warranty_expiry and start:
                warranty_expiry = start + timedelta(days=(code.warranty_days or 30) + (code.extension_days or 0))
        # 兼容仅有 used_team_id/used_by_email 的旧质保码绑定。
        protected = member.get('departure_policy') == 'team_expiry'
        deadline = member.get('auto_kick_at')
        if member.get('role') == 'account-owner':
            state, label, reason = 'owner', '所有者', '不参与自动下线'
        elif member.get('status') == 'invited':
            state, label, reason = 'invited', '等待确认邀请', '确认加入后开始计时'
        elif protected and team.expires_at and team.expires_at <= now:
            state, label, reason = 'overdue', '到期待下线', 'Team 已到期，等待自动下线'
        elif protected:
            if warranty_expiry and warranty_expiry <= now:
                state, label = 'protected', '质保已结束'
            elif warranty_expiry:
                state, label = 'warranty', '质保中'
            else:
                state, label = 'protected', '质保信息待同步'
            reason = '免小时级及额度清退，Team 到期后下线' if team.expires_at else 'Team 到期时间待同步'
        elif member.get('auto_kick_enabled') is False:
            state, label, reason = 'retained', '保留成员', '不参与自动下线'
        elif deadline:
            overdue = deadline <= iso(now)
            state, label = ('overdue', '到期待下线') if overdue else ('timed', '定时使用中')
            reason = '按使用时长下线'
            if member.get('seat_type') == 'premium' and team.rotation_mode == 'auto':
                reason += '；额度耗尽可能提前轮转'
        else:
            state, label, reason = 'syncing', '等待同步', '使用起点待同步'
        member.update(seat_status=state, status_label=label, status_reason=reason,
                      warranty_expires_at=iso(warranty_expiry), team_expires_at=iso(team.expires_at),
                      server_now=iso(now))
    return members
