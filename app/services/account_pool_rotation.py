"""Read-only rotation predictions using the full pool, independent of pagination."""
from collections import defaultdict
from datetime import timedelta

import pytz

from app.config import settings
from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.models import (
    AccountPoolEntry, AccountPoolHistory, MemberAuthorization, QuotaSnapshot,
    RotationAction, RotationMemberState, RotationSeatSnapshot, Sub2apiExportRecord,
    Team, TeamEmailMapping, TeamReplacementQueue,
)
from app.services.account_pool import TEAM_REINVITE_COOLDOWN_DAYS
from app.services.quota_rotation_policy import ADMIN_ROLES, MAX_SNAPSHOT_AGE, next_action, quota_ready
from app.services.rotation_candidates import rotation_candidate_block_reason
from app.utils.time_utils import get_now
from app.utils.team_names import team_display_name

OPEN_STATUSES = ("pending", "executing", "reconciling")
REASONS = {
    "invite_pending": "已有邀请任务待确认",
    "already_exported": "已向目标 Team 导出过",
    "import_uncertain": "Sub2API 导入结果待核对",
    "quota_not_ready": "曾加入目标 Team，需确认周额度已恢复",
    "cooldown": "目标 Team 的 7 天邀请冷却尚未结束",
    "SUB2API_PAUSE_UNVERIFIED": "需先确认 Sub2API 账号已暂停",
    "BLOCKED_NO_STANDARD_SEAT": "等待标准席位空位",
}


def _view(label, reason, team=None, *, kind="waiting", mode=None):
    return {"label": label, "reason": reason, "kind": kind,
            "team_id": team.id if team else None,
            "team_name": team_display_name(team) if team else None,
            "mode": mode or (team.rotation_mode if team else None)}


def _balance(snapshot, now):
    if snapshot is None or snapshot.observed_at < now - MAX_SNAPSHOT_AGE:
        return {"success": False}
    return {"success": True, "balance": {
        kind: {"known": True, "remaining": getattr(snapshot, f"{kind}_remaining")}
        for kind in ("standard", "premium")
    }}


def _replacement_wait_reason(item, seat_label, balance, remaining, now):
    if item.last_attempt_at and item.last_attempt_at >= now - MAX_SNAPSHOT_AGE and item.last_error_code:
        reasons = {
            "seat_limit_exceeded": f"等待{seat_label}席位空位",
            "seat_balance_unavailable": "等待席位余额确认",
            "seat_operation_pending": "等待上次席位操作确认",
            "ghost_success": "邀请未在上游确认，等待重试",
            "no_candidate": "等待后台重新确认补位账号",
        }
        return reasons.get(item.last_error_code, "上次邀请失败，等待后台重试"), True
    if not balance["success"]:
        return "等待席位余额确认", True
    if remaining <= 0:
        return f"等待{seat_label}席位空位", True
    return "等待后台发出邀请", False


def _member_view(mapping, team, snapshot, state, exported, now):
    if mapping.status == "invited":
        return _view("待接受邀请", "已邀请至此 Team，接受后继续授权导出", team)
    if team.rotation_mode == "off":
        if mapping.member_role == "account-owner" or mapping.auto_kick_exempt:
            return _view("保留当前 Team", "已免除定时下线", team, kind="idle")
        if not mapping.auto_kick_at or not mapping.upstream_user_id:
            return _view("待确认下线计划", "退出当前 Team 后再匹配下一站", team)
        view = _view("等待退出" if mapping.auto_kick_at <= now else "定时下线",
                     "下线时间 " + mapping.auto_kick_at.strftime("%m-%d %H:%M"), team)
        view["exit_at"] = pytz.timezone(settings.timezone).localize(mapping.auto_kick_at).isoformat()
        return view
    if state and state.blocked_reason:
        return _view("轮转受阻", REASONS.get(state.blocked_reason, "等待轮转问题处理"), team, kind="blocked")
    if not quota_ready(mapping, snapshot):
        return _view("额度待确认", "等待当前席位的最新周额度", team)
    if snapshot.weekly_remaining > 0:
        return _view("继续使用", "周额度尚未耗尽，保留当前 Team", team, kind="idle")
    if state and state.phase in ("wait_reset", "removed"):
        return _view("等待额度恢复", "周额度恢复后重新参与轮转", team)
    if mapping.seat_type == "standard":
        return _view("待升高级", "在当前 Team 按排队顺序等待高级席位", team)
    if mapping.seat_type == "premium":
        if mapping.member_role in ADMIN_ROLES or mapping.auto_kick_exempt:
            return _view("待降标准", "在当前 Team 等待标准空位，保留账号", team)
        if exported:
            return _view("轮转受阻", REASONS["SUB2API_PAUSE_UNVERIFIED"], team, kind="blocked")
        return _view("待退出后匹配", "高级周额度耗尽；退出后才会选择下一 Team")
    return _view("席位待确认", "当前席位类型未知", team)


async def attach_rotation_status(db, rows):
    if not rows:
        return rows
    now = get_now()
    # Fixed batched reads: no network requests, credential reads, or mutations.
    teams = {team.id: team for team in (await db.execute(select(Team).options(load_only(
        Team.id, Team.email, Team.team_name, Team.account_id, Team.rotation_mode, Team.status, Team.pending_replacements,
    )).order_by(Team.id))).scalars()}
    candidates = (await db.execute(select(
        AccountPoolEntry.id, AccountPoolEntry.email, AccountPoolEntry.updated_at,
    ).where(AccountPoolEntry.deleted_at.is_(None)).order_by(
        AccountPoolEntry.updated_at, AccountPoolEntry.id,
    ))).all()
    mappings = (await db.execute(select(TeamEmailMapping))).scalars().all()
    by_email, by_team, invited_at = defaultdict(list), defaultdict(list), {}
    for mapping in mappings:
        if mapping.last_invited_at:
            invited_at[(mapping.email, mapping.team_id)] = mapping.last_invited_at
        if mapping.status in ("joined", "invited"):
            by_email[mapping.email].append(mapping)
            by_team[mapping.team_id].append(mapping)
    quotas = {(s.email, s.team_space_id): s for s in
              (await db.execute(select(QuotaSnapshot))).scalars()}
    seats = {s.team_id: s for s in (await db.execute(select(RotationSeatSnapshot))).scalars()}
    states = {(s.team_id, s.email): s for s in
              (await db.execute(select(RotationMemberState))).scalars()}
    histories = set((await db.execute(select(AccountPoolHistory.account_pool_id,
                                            AccountPoolHistory.team_id))).all())
    exported = set((await db.execute(select(Sub2apiExportRecord.email,
                                           Sub2apiExportRecord.team_space_id))).all())
    uncertain = set((await db.execute(select(MemberAuthorization.email, MemberAuthorization.account_id).where(
        MemberAuthorization.sub2api_import_uncertain.is_(True),
    ))).all())
    actions = (await db.execute(select(RotationAction).where(
        RotationAction.status.in_(OPEN_STATUSES),
    ).order_by(RotationAction.id))).scalars().all()
    by_action_email, busy_teams = {}, set()
    for action in actions:
        by_action_email.setdefault(action.email, action)
        busy_teams.add(action.team_id)
    in_flight = {action.email for action in actions if action.action_type == "invite"}
    queues = defaultdict(list)
    for item in (await db.execute(select(TeamReplacementQueue).order_by(TeamReplacementQueue.id))).scalars():
        queues[item.team_id].append(item)

    owners = {team.email.strip().lower() for team in teams.values()}
    available = [entry for entry in candidates if not by_email[entry.email] and entry.email not in owners]
    planned, rejected = {}, defaultdict(set)
    waiting = set()
    # Auto predictions take precedence over dry-run previews. Each pass reserves
    # a candidate once, so different Teams do not claim the same account.
    deadlines = defaultdict(list)
    for mapping in mappings:
        team = teams.get(mapping.team_id)
        if (team and team.rotation_mode == "off" and mapping.status == "joined"
                and mapping.auto_kick_at and mapping.upstream_user_id
                and not mapping.auto_kick_exempt and mapping.member_role is not None
                and mapping.member_role != "account-owner"
                and mapping.email.strip().lower() not in owners):
            deadlines[team.id].append(mapping)
    for members in deadlines.values():
        members.sort(key=lambda m: (m.auto_kick_at, m.id))
    for mode_group in (("off", "auto"), ("scheduled",), ("dry_run",)):
        reserved = set(planned) | in_flight
        scheduled = mode_group == ("scheduled",)
        ordered_teams = sorted(teams.values(), key=lambda team: (
            deadlines[team.id][0].auto_kick_at if deadlines[team.id] else now + timedelta(days=36500), team.id
        )) if scheduled else teams.values()
        for team in ordered_teams:
            if (team.rotation_mode != "off" if scheduled else team.rotation_mode not in mode_group):
                continue
            legacy = team.rotation_mode == "off"
            if scheduled and (not deadlines[team.id] or queues[team.id] or team.pending_replacements):
                continue
            if legacy and not scheduled and not (queues[team.id] or team.pending_replacements):
                continue
            if team.status not in ("active", "full"):
                waiting.add("目标 Team 当前不可用")
                continue
            if team.id in busy_teams:
                waiting.add("等待已有轮转动作确认")
                continue
            balance = _balance(seats.get(team.id), now)
            members = by_team[team.id]
            if scheduled:
                slots = [m.seat_type or "standard" for m in deadlines[team.id]]
            elif legacy:
                slots = [item.seat_type for item in queues[team.id]]
                if not slots:
                    waiting.add("等待补位队列确认")
                    continue
            else:
                if not balance["success"]:
                    waiting.add("等待最新席位余额")
                    continue
                decision = next_action(
                    [m for m in members if m.status == "joined"],
                    {m.email: quotas.get((m.email, team.account_id)) for m in members},
                    {m.email: states.get((team.id, m.email)) for m in members}, balance,
                )
                if decision:
                    waiting.add("等待 Team 内成员完成席位轮转")
                    continue
                if any(m.status == "invited" for m in members):
                    waiting.add("等待已有邀请被接受")
                    continue
                slots = ["standard"]
            remaining = {kind: balance["balance"][kind]["remaining"] if balance["success"] and not scheduled else None
                         for kind in ("standard", "premium")}
            blocked_seats = set()
            for slot_index, seat_type in enumerate(slots):
                if seat_type in blocked_seats:
                    continue
                if seat_type not in remaining or (not legacy and remaining[seat_type] is not None and remaining[seat_type] <= 0):
                    waiting.add("等待可用席位")
                    break
                chosen = None
                for entry in available:
                    if entry.email in reserved:
                        continue
                    last_invited = invited_at.get((entry.email, team.id))
                    reason = "cooldown" if last_invited and last_invited > now - timedelta(
                        days=TEAM_REINVITE_COOLDOWN_DAYS) else None
                    if not reason and not legacy:
                        reason = rotation_candidate_block_reason(
                            in_flight=entry.email in in_flight,
                            exported=(entry.email, team.account_id) in exported,
                            uncertain=(entry.email, team.account_id) in uncertain,
                            has_history=(entry.id, team.id) in histories,
                            snapshot=quotas.get((entry.email, team.account_id)), now=now,
                        )
                    if reason:
                        rejected[entry.email].add(REASONS[reason])
                        continue
                    chosen = entry
                    break
                if chosen is None:
                    break
                reserved.add(chosen.email)
                seat_remaining = remaining[seat_type]
                if seat_remaining is not None:
                    remaining[seat_type] -= 1
                seat_label = "高级" if seat_type == "premium" else "标准"
                waiting_reason, blocked = (
                    _replacement_wait_reason(queues[team.id][slot_index], seat_label, balance, seat_remaining, now)
                    if legacy and not scheduled else (None, False)
                )
                planned[chosen.email] = _view(
                    "候选补位" if scheduled or blocked or not balance["success"] else (
                        "仅预览" if team.rotation_mode == "dry_run" else "预计加入"),
                    f"{seat_label}席位 · {'定时补位' if scheduled else '等待补位' if legacy else '额度轮转'}，" + (
                        "等待当前成员下线后释放席位" if scheduled else waiting_reason or
                        "执行前会重新确认"),
                    team, kind="ready" if balance["success"] and not scheduled and not blocked else "candidate",
                )

                if legacy:
                    view = planned[chosen.email]
                    view["scheduled_replacement"] = True
                    if scheduled:
                        deadline = deadlines[team.id][slot_index].auto_kick_at
                        view["replacement_at"] = pytz.timezone(settings.timezone).localize(deadline).isoformat()
                    else:
                        view["replacement_at"] = None
                if blocked:
                    # 同席位保持 FIFO，标准席位受阻不影响高级席位。
                    blocked_seats.add(seat_type)

    for row in rows:
        email = row["email"]
        active = by_email[email]
        action = by_action_email.get(email)
        if row.get("is_owner"):
            view = _view("所有者", "", kind="idle")
        elif action:
            labels = {"invite": "邀请确认中", "remove": "退出确认中",
                      "upgrade": "升级确认中", "return_standard": "降级确认中"}
            view = _view(labels.get(action.action_type, "轮转处理中"),
                         "等待上游确认，暂不安排其他 Team", teams.get(action.team_id))
        elif len({m.team_id for m in active}) > 1:
            view = _view("归属冲突", "当前关联多个 Team，需先确认归属", kind="blocked")
        elif active:
            mapping = active[0]
            team = teams.get(mapping.team_id)
            view = _member_view(mapping, team, quotas.get((email, team.account_id)),
                                states.get((team.id, email)),
                                (email, team.account_id) in exported, now) if team else _view(
                                    "归属待确认", "原 Team 已不存在", kind="blocked")
            if team and team.rotation_mode == "dry_run":
                view["reason"] = "仅预览；" + view["reason"]
        elif email in planned:
            view = planned[email]
        else:
            reasons = sorted(rejected[email])
            if reasons:
                view = _view("等待资格恢复", "；".join(reasons))
            elif planned:
                view = _view("等待排队", "当前空位优先分配给更早进入队列的账号")
            else:
                view = _view("待分配", "；".join(sorted(waiting)) or "暂无开启自动轮转或等待补位的 Team")
        row["rotation"] = view
    return rows
