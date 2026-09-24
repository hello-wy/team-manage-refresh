"""Refresh workspace-scoped quota observations independently of page views."""

from sqlalchemy import select

from app.models import (AccountPoolEntry, AccountPoolHistory, QuotaSnapshot,
                        Sub2apiExportRecord, Team, TeamEmailMapping)
from app.utils.seats import normalize_seat_type
from app.utils.time_utils import get_now


def _window_values(window):
    if not window:
        return None, None, None
    return window.get("remaining"), window.get("limit"), window.get("reset_at")


async def store_quota(db, *, email, space_id, seat_type, usage):
    snapshot = (await db.execute(select(QuotaSnapshot).where(
        QuotaSnapshot.email == email, QuotaSnapshot.team_space_id == space_id,
    ))).scalar_one_or_none()
    if snapshot is None:
        snapshot = QuotaSnapshot(email=email, team_space_id=space_id, status="unknown")
        db.add(snapshot)
    short = _window_values(usage.get("5h"))
    weekly = _window_values(usage.get("1week"))
    snapshot.observed_seat_type = seat_type
    snapshot.short_remaining, snapshot.short_limit, snapshot.short_reset_at = short
    snapshot.weekly_remaining, snapshot.weekly_limit, snapshot.weekly_reset_at = weekly
    snapshot.status = usage["status"]
    snapshot.error = usage.get("error")
    snapshot.observed_at = get_now()
    snapshot.version = (snapshot.version or 0) + 1
    return snapshot


async def refresh_team(db, team: Team, team_service, *, usage_service):
    members = await team_service.get_team_members(team.id, db)
    balance = members.get("seat_balance") or {}
    if not members.get("success") or not balance.get("success") or not all(
        (balance.get("balance") or {}).get(kind, {}).get("known")
        for kind in ("standard", "premium")
    ):
        raise RuntimeError(members.get("error") or "成员列表或席位余额不完整")
    mappings = (await db.execute(select(TeamEmailMapping).where(
        TeamEmailMapping.team_id == team.id,
        TeamEmailMapping.status == "joined",
    ))).scalars().all()
    keys = [(row.email, team.account_id) for row in mappings]
    usage_by_key = await usage_service.check_many(db, keys)
    for mapping in mappings:
        usage = usage_by_key[(mapping.email, team.account_id)]
        await store_quota(db, email=mapping.email, space_id=team.account_id,
                          seat_type=normalize_seat_type(mapping.seat_type), usage=usage)
    await db.commit()
    return len(mappings)


async def refresh_export_snapshots(db, usage_service):
    records = (await db.execute(select(Sub2apiExportRecord))).scalars().all()
    enabled = set((await db.execute(select(Team.id).where(
        Team.rotation_mode != "off"))).scalars().all())
    by_key = {(row.email, row.team_space_id): row.seat_type or "unknown"
              for row in records if row.team_id not in enabled}
    keys = list(by_key)
    if not keys:
        return 0
    usages = await usage_service.check_many(db, keys)
    for email, space_id in keys:
        await store_quota(db, email=email, space_id=space_id,
                          seat_type=normalize_seat_type(by_key[(email, space_id)]),
                          usage=usages[(email, space_id)])
    await db.commit()
    return len(keys)


async def refresh_historical_candidates(db, usage_service):
    active = select(TeamEmailMapping.id).where(
        TeamEmailMapping.team_id == Team.id,
        TeamEmailMapping.email == AccountPoolEntry.email,
        TeamEmailMapping.status.in_(("joined", "invited")),
    )
    rows = (await db.execute(select(AccountPoolEntry.email, Team.account_id).join(
        AccountPoolHistory, AccountPoolHistory.account_pool_id == AccountPoolEntry.id,
    ).join(Team, Team.id == AccountPoolHistory.team_id).where(
        Team.rotation_mode != "off", AccountPoolHistory.left_at.is_not(None),
        AccountPoolEntry.deleted_at.is_(None), ~active.exists(),
    ).distinct())).all()
    keys = [(email, space_id) for email, space_id in rows if space_id]
    if not keys:
        return 0
    usages = await usage_service.check_many(db, keys)
    for email, space_id in keys:
        await store_quota(db, email=email, space_id=space_id,
                          seat_type="unknown", usage=usages[(email, space_id)])
    await db.commit()
    return len(keys)
