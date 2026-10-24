"""Read-only rotation status for account pool rows, independent of pagination."""
from collections import defaultdict

import pytz

from app.config import settings
from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.models import (
    QuotaSnapshot, RotationAction, RotationMemberState, Sub2apiExportRecord, Team, TeamEmailMapping,
)
from app.services.member_auto_kick_policy import has_auto_kick_schedule, participates
from app.services.quota_rotation_policy import ADMIN_ROLES, quota_ready
from app.utils.time_utils import get_now
from app.utils.team_names import team_display_name

OPEN_STATUSES = ("pending", "executing", "reconciling")
ROTATION_FILTER_OPTIONS = {
    "invited": "待接受邀请",
    "using": "使用中",
    "waiting_exit": "待下线",
    "waiting_rotation": "待轮转",
    "processing": "处理中",
    "attention": "需关注",
    "idle": "空闲",
    "excluded": "不参与轮转",
}
# Internal checks retain their precise reasons; list badges and filters use a
# small set of lifecycle groups instead of exposing every implementation step.
_STATUS_BY_LABEL = {
    "待接受邀请": "invited",
    "继续使用": "using",
    "定时下线": "waiting_exit", "等待退出": "waiting_exit",
    "待升高级": "waiting_rotation", "待降标准": "waiting_rotation",
    "等待额度恢复": "waiting_rotation",
    "邀请确认中": "processing", "退出确认中": "processing",
    "升级确认中": "processing", "降级确认中": "processing", "轮转处理中": "processing",
    "下线失败": "attention", "轮转受阻": "attention", "归属冲突": "attention",
    "额度待确认": "attention", "待确认下线计划": "attention",
    "席位待确认": "attention", "归属待确认": "attention",
    "空闲": "idle",
    "所有者": "excluded", "保留当前 Team": "excluded",
}
REASONS = {
    "SUB2API_PAUSE_UNVERIFIED": "需先确认 Sub2API 账号已暂停",
    "BLOCKED_NO_STANDARD_SEAT": "等待标准席位空位",
}


def _view(label, reason, team=None, *, kind="waiting", mode=None):
    status = _STATUS_BY_LABEL[label]
    group_label = ROTATION_FILTER_OPTIONS[status]
    if label != group_label:
        reason = label + ("；" + reason if reason else "")
    return {"status": status, "label": group_label, "detail": label, "reason": reason, "kind": kind,
            "team_id": team.id if team else None,
            "team_name": team_display_name(team) if team else None,
            "mode": mode or (team.rotation_mode if team else None)}


def _schedule_view(mapping, team, now):
    if mapping.auto_kick_last_error:
        retry = mapping.auto_kick_retry_at
        reason = mapping.auto_kick_last_error + (f"；{retry.strftime('%m-%d %H:%M')} 重试" if retry else "")
        return _view("下线失败", reason, team, kind="blocked")
    reason = "下线时间 " + mapping.auto_kick_at.strftime("%m-%d %H:%M") + "；退出后不会自动补位"
    if getattr(mapping, "departure_policy", None) == "team_expiry":
        reason = "Team 到期下线 " + mapping.auto_kick_at.strftime("%m-%d %H:%M")
    elif team.rotation_mode != "off":
        reason += "；额度轮转照常进行"
    view = _view("等待退出" if mapping.auto_kick_at <= now else "定时下线", reason, team)
    view["exit_at"] = pytz.timezone(settings.timezone).localize(mapping.auto_kick_at).isoformat()
    return view


def _member_view(mapping, team, snapshot, state, exported, now):
    if mapping.status == "invited":
        return {**_view("待接受邀请", "已邀请至此 Team，接受后继续授权导出", team),
                "can_import_16688": True}
    if has_auto_kick_schedule(mapping):
        return _schedule_view(mapping, team, now)
    if team.rotation_mode == "off":
        if not participates(mapping):
            return _view("保留当前 Team", "未参与自动下线", team, kind="idle")
        return _view("待确认下线计划", "等待同步成员加入时间", team)
    if state and state.blocked_reason:
        return _view("轮转受阻", REASONS.get(state.blocked_reason, "等待轮转问题处理"), team, kind="blocked")
    if not quota_ready(mapping, snapshot):
        return _view("额度待确认", "等待当前席位的最新周额度", team)
    if snapshot.weekly_remaining > 0:
        return _view("继续使用", "周额度尚未耗尽，保留当前 Team", team, kind="idle")
    if state and state.phase in ("wait_reset", "removed"):
        return _view("等待额度恢复", "周额度恢复前保持当前状态", team)
    if mapping.seat_type == "standard":
        return _view("待升高级", "在当前 Team 按排队顺序等待高级席位", team)
    if mapping.seat_type == "premium":
        if mapping.member_role in ADMIN_ROLES or mapping.auto_kick_exempt:
            return _view("待降标准", "在当前 Team 等待标准空位，保留账号", team)
        if exported:
            return _view("轮转受阻", REASONS["SUB2API_PAUSE_UNVERIFIED"], team, kind="blocked")
        return _view("等待退出", "高级周额度耗尽；退出后不会自动补位", team)
    return _view("席位待确认", "当前席位类型未知", team)


async def attach_rotation_status(db, rows):
    if not rows:
        return rows
    now = get_now()
    # Fixed batched reads: no network requests, credential reads, or mutations.
    teams = {team.id: team for team in (await db.execute(select(Team).options(load_only(
        Team.id, Team.email, Team.team_name, Team.account_id, Team.rotation_mode, Team.status,
    )).order_by(Team.id))).scalars()}
    mappings = (await db.execute(select(TeamEmailMapping).where(
        TeamEmailMapping.status.in_(("joined", "invited")),
    ))).scalars().all()
    by_email = defaultdict(list)
    for mapping in mappings:
        by_email[mapping.email].append(mapping)
    quotas = {(s.email, s.team_space_id): s for s in
              (await db.execute(select(QuotaSnapshot))).scalars()}
    states = {(s.team_id, s.email): s for s in
              (await db.execute(select(RotationMemberState))).scalars()}
    exported = set((await db.execute(select(Sub2apiExportRecord.email,
                                           Sub2apiExportRecord.team_space_id))).all())
    actions = (await db.execute(select(RotationAction).where(
        RotationAction.status.in_(OPEN_STATUSES),
    ).order_by(RotationAction.id))).scalars().all()
    by_action_email = {}
    for action in actions:
        by_action_email.setdefault(action.email, action)

    owner_emails = {team.email.strip().lower() for team in teams.values()}
    for row in rows:
        email = row["email"]
        active = by_email[email]
        action = by_action_email.get(email)
        if row.get("is_owner", email in owner_emails):
            view = _view("所有者", "", kind="idle")
        elif action:
            labels = {"invite": "邀请确认中", "remove": "退出确认中",
                      "upgrade": "升级确认中", "return_standard": "降级确认中"}
            view = _view(labels.get(action.action_type, "轮转处理中"),
                         "等待上游确认", teams.get(action.team_id))
        elif len({m.team_id for m in active}) > 1:
            view = _view("归属冲突", "当前关联多个 Team，需先确认归属", kind="blocked")
        elif active:
            mapping = active[0]
            team = teams.get(mapping.team_id)
            view = _member_view(mapping, team, quotas.get((email, team.account_id)),
                                states.get((team.id, email)),
                                (email, team.account_id) in exported, now) if team else _view(
                                    "归属待确认", "原 Team 已不存在", kind="blocked")
            if team and team.rotation_mode == "dry_run" and not has_auto_kick_schedule(mapping):
                view["reason"] = "仅预览；" + view["reason"]
        else:
            view = _view("空闲", "未加入 Team；不会自动邀请，可手动加入", kind="idle")
        row["rotation"] = view
    return rows
