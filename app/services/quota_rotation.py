"""Persist and reconcile one quota-driven Team mutation at a time."""

import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.models import (AccountPoolHistory, MemberAuthorization, QuotaSnapshot, RotationAction,
                        RotationLease, RotationMemberState, Team, TeamEmailMapping)
from app.models import Sub2apiExportRecord
from app.services.account_pool_replacement import mark_replacement_pending
from app.services.quota_rotation_policy import ADMIN_ROLES, next_action
from app.services.quota_sync import refresh_team
from app.utils.time_utils import get_now

LEASE_DURATION = timedelta(minutes=10)
OPEN_STATUSES = ("pending", "executing", "reconciling", "blocked")


@dataclass(frozen=True)
class RotationDependencies:
    teams: object
    usage: object
    pool: object


async def acquire_lease(db, resource_key):
    now = get_now()
    holder = str(uuid4())
    changed = await db.execute(update(RotationLease).where(
        RotationLease.resource_key == resource_key, RotationLease.expires_at <= now,
    ).values(holder=holder, expires_at=now + LEASE_DURATION,
             version=RotationLease.version + 1))
    if changed.rowcount:
        await db.commit()
        return holder
    db.add(RotationLease(resource_key=resource_key, holder=holder,
                         expires_at=now + LEASE_DURATION))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    return holder


async def release_lease(db, resource_key, holder):
    await db.execute(delete(RotationLease).where(
        RotationLease.resource_key == resource_key, RotationLease.holder == holder))
    await db.commit()


async def load_rotation_data(db, team):
    mappings = (await db.execute(select(TeamEmailMapping).where(
        TeamEmailMapping.team_id == team.id,
        TeamEmailMapping.status == "joined",
    ))).scalars().all()
    snapshots = (await db.execute(select(QuotaSnapshot).where(
        QuotaSnapshot.team_space_id == team.account_id,
        QuotaSnapshot.email.in_([row.email for row in mappings]),
    ))).scalars().all()
    states = (await db.execute(select(RotationMemberState).where(
        RotationMemberState.team_id == team.id,
    ))).scalars().all()
    return mappings, {row.email: row for row in snapshots}, {row.email: row for row in states}


async def record_stages(db, team, mappings, *, snapshots, states, seat_balance):
    now = get_now()
    for mapping in mappings:
        state = states.get(mapping.email)
        if state is None:
            state = RotationMemberState(team_id=team.id, email=mapping.email)
            db.add(state)
            states[mapping.email] = state
        snapshot = snapshots.get(mapping.email)
        if not snapshot or snapshot.status != "ok" or snapshot.observed_seat_type != mapping.seat_type:
            continue
        if state.phase in ("wait_reset", "wait_premium") and snapshot.weekly_remaining is not None and snapshot.weekly_remaining > 0:
            state.phase = "standard"
            state.cycle_id += 1
            state.standard_completed_at = None
            state.premium_completed_at = None
            state.queued_at = None
            state.blocked_reason = None
        if snapshot.weekly_remaining != 0:
            state.blocked_reason = None
            continue
        if mapping.seat_type == "standard" and not state.standard_completed_at:
            state.standard_completed_at = now
            state.queued_at = now
            state.phase = "wait_premium"
        if mapping.seat_type == "premium" and not state.premium_completed_at:
            state.premium_completed_at = now
        if mapping.seat_type == "premium" and mapping.member_role in ADMIN_ROLES:
            standard = (seat_balance.get("balance") or {}).get("standard", {})
            state.blocked_reason = "BLOCKED_NO_STANDARD_SEAT" if standard.get("remaining") == 0 else None
    await db.commit()


async def pending_action(db, team_id):
    return (await db.execute(select(RotationAction).where(
        RotationAction.team_id == team_id, RotationAction.status.in_(OPEN_STATUSES),
    ).order_by(RotationAction.id.asc()))).scalars().first()


def action_confirmed(action, members):
    member = next((row for row in members if row.get("email", "").lower() == action.email), None)
    if action.action_type == "remove":
        return member is None
    if action.action_type == "upgrade":
        return member is not None and member.get("seat_type") in ("premium", "prolite")
    if action.action_type == "return_standard":
        return member is not None and member.get("seat_type") in ("default", "standard")
    if action.action_type == "invite":
        return member is not None and member.get("status") in ("invited", "joined")
    return False


async def reconcile_action(db, action, team_service):
    result = await team_service.get_team_members(action.team_id, db)
    if not result.get("success"):
        action.status = "reconciling"
        action.result = result.get("error")
    elif action_confirmed(action, result["members"]):
        action.status = "succeeded"
        action.result = "上游成员列表已确认"
        await _complete_action(db, action)
    elif action.status in ("executing", "reconciling"):
        action.status = "blocked"
        action.result = "上游未确认变更；请核对后人工处理，避免重复提交"
    await db.commit()
    return action.status


async def _complete_action(db, action):
    state = (await db.execute(select(RotationMemberState).where(
        RotationMemberState.team_id == action.team_id,
        RotationMemberState.email == action.email,
    ))).scalar_one_or_none()
    if action.action_type == "invite":
        await mark_replacement_pending(db, action.team_id, action.email)
    if not state:
        return
    if action.action_type == "upgrade":
        state.phase = "premium"
    elif action.action_type == "return_standard":
        state.phase = "wait_reset"
        state.standard_completed_at = None
        state.queued_at = None
    elif action.action_type == "remove":
        state.phase = "removed"
        state.cycle_id += 1


async def _candidate(db, team, pool):
    excluded = set()
    while candidate := await pool.find_replacement_candidate(
        team.id, db, excluded_ids=frozenset(excluded)
    ):
        excluded.add(candidate.id)
        history = (await db.execute(select(AccountPoolHistory.id).where(
            AccountPoolHistory.account_pool_id == candidate.id,
            AccountPoolHistory.team_id == team.id,
        ).limit(1))).scalar_one_or_none()
        exported = (await db.execute(select(Sub2apiExportRecord.id).where(
            Sub2apiExportRecord.email == candidate.email,
            Sub2apiExportRecord.team_space_id == team.account_id,
        ).limit(1))).scalar_one_or_none()
        if exported:
            continue
        uncertain = (await db.execute(select(MemberAuthorization.id).where(
            MemberAuthorization.email == candidate.email,
            MemberAuthorization.account_id == team.account_id,
            MemberAuthorization.sub2api_import_uncertain.is_(True),
        ).limit(1))).scalar_one_or_none()
        if uncertain:
            continue
        if not history:
            return candidate
        snapshot = (await db.execute(select(QuotaSnapshot).where(
            QuotaSnapshot.email == candidate.email,
            QuotaSnapshot.team_space_id == team.account_id,
        ))).scalar_one_or_none()
        if snapshot and snapshot.status == "ok" and snapshot.weekly_remaining:
            return candidate
    return None


async def _execute_remote(db, team, action, *, mapping, team_service):
    if action.action_type == "invite":
        return await team_service.add_team_member(team.id, action.email, db, seat_type="default")
    if not mapping or not mapping.upstream_user_id:
        raise RuntimeError("成员 ID 缺失，无法确认目标")
    if action.action_type == "remove":
        if mapping.member_role in ADMIN_ROLES or mapping.auto_kick_exempt:
            raise RuntimeError("受保护成员不能退出 Team")
        return await team_service.delete_team_member(
            team.id, mapping.upstream_user_id, db, email=action.email)
    target = "premium" if action.action_type == "upgrade" else "default"
    return await team_service.update_member_seat_type(
        team.id, mapping.upstream_user_id, target, mapping.seat_type, db)


async def execute_action(db, team, decision, *, mapping, team_service):
    state = (await db.execute(select(RotationMemberState).where(
        RotationMemberState.team_id == team.id,
        RotationMemberState.email == decision.email,
    ))).scalar_one_or_none()
    cycle = state.cycle_id if state else 1
    key = f"{team.id}:{decision.email}:{cycle}:{decision.action}"
    action = (await db.execute(select(RotationAction).where(
        RotationAction.idempotency_key == key))).scalar_one_or_none()
    if action:
        return {"status": action.status, "action_id": action.id}
    action = RotationAction(team_id=team.id, email=decision.email,
                            action_type=decision.action, idempotency_key=key,
                            reason=decision.reason, status="executing", attempts=1)
    db.add(action)
    await db.commit()
    try:
        result = await _execute_remote(db, team, action, mapping=mapping, team_service=team_service)
        action.result = json.dumps({"success": result.get("success"),
                                    "status": result.get("status"),
                                    "error": result.get("error")}, ensure_ascii=False)
        action.status = "reconciling"
        await db.commit()
    except Exception as exc:
        action.status = "reconciling"
        action.result = str(exc)
        await db.commit()
    status = await reconcile_action(db, action, team_service)
    return {"status": status, "action_id": action.id}


async def run_team(db, team, deps: RotationDependencies):
    resource_key = f"workspace:{team.account_id}"
    holder = await acquire_lease(db, resource_key)
    if not holder:
        return {"status": "busy"}
    try:
        return await _run_locked(db, team, deps)
    finally:
        await release_lease(db, resource_key, holder)


async def _run_locked(db, team, deps: RotationDependencies):
    pending = await pending_action(db, team.id)
    if pending:
        status = await reconcile_action(db, pending, deps.teams)
        return {"status": status, "action_id": pending.id}
    await refresh_team(db, team, deps.teams, usage_service=deps.usage)
    members_result = await deps.teams.get_team_members(team.id, db)
    balance = members_result.get("seat_balance") or {}
    if not members_result.get("success") or not balance.get("success") or not all(
        (balance.get("balance") or {}).get(kind, {}).get("known")
        for kind in ("standard", "premium")
    ):
        raise RuntimeError("成员列表或已购席位余额不完整")
    mappings, snapshots, states = await load_rotation_data(db, team)
    await record_stages(db, team, mappings, snapshots=snapshots, states=states,
                        seat_balance=balance)
    decision = next_action(mappings, snapshots, states, balance)
    if team.rotation_mode == "dry_run":
        return {"status": "preview", "next_action": vars(decision) if decision else None}
    if decision is None:
        standard = members_result["seat_balance"]["balance"]["standard"]["remaining"]
        if standard and not any(row.get("status") == "invited" for row in members_result["members"]):
            candidate = await _candidate(db, team, deps.pool)
            if candidate:
                from app.services.quota_rotation_policy import RotationDecision
                decision = RotationDecision(candidate.email, "invite", "标准席位空缺")
    if decision is None:
        return {"status": "idle"}
    mapping = next((row for row in mappings if row.email == decision.email), None)
    if decision.action != "invite":
        return await execute_action(db, team, decision, mapping=mapping, team_service=deps.teams)
    resource = f"candidate:{decision.email}"
    holder = await acquire_lease(db, resource)
    if not holder:
        return {"status": "candidate_busy"}
    try:
        active = (await db.execute(select(TeamEmailMapping.id).where(
            TeamEmailMapping.email == decision.email,
            TeamEmailMapping.status.in_(("joined", "invited")),
        ).limit(1))).scalar_one_or_none()
        if active:
            return {"status": "candidate_busy"}
        return await execute_action(db, team, decision, mapping=mapping, team_service=deps.teams)
    finally:
        await release_lease(db, resource, holder)
