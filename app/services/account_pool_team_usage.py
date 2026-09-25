"""Persist seat switches and quota refresh state for account-pool Teams."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolTeamUsage, Team
from app.utils.time_utils import get_now


def _reset_at(usage: dict[str, Any] | None, key: str) -> str | None:
    return ((usage or {}).get(key) or {}).get("reset_at")


async def _load_row(
    db: AsyncSession,
    *,
    email: str,
    team: Team,
) -> AccountPoolTeamUsage | None:
    entry = (await db.execute(select(AccountPoolEntry).where(
        AccountPoolEntry.email == email,
        AccountPoolEntry.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if entry is None:
        return None
    row = (await db.execute(select(AccountPoolTeamUsage).where(
        AccountPoolTeamUsage.account_pool_id == entry.id,
        AccountPoolTeamUsage.team_id == team.id,
    ))).scalar_one_or_none()
    if row is None:
        row = AccountPoolTeamUsage(
            account_pool_id=entry.id,
            team_id=team.id,
            team_space_id=team.account_id,
        )
        db.add(row)
        await db.flush()
    return row


async def record_seat_switch(
    db: AsyncSession,
    *,
    team_id: int,
    email: str,
    seat_type: str,
) -> AccountPoolTeamUsage | None:
    """Record a confirmed or pending change to a member's seat type."""
    normalized_email = email.strip().lower()
    if not normalized_email:
        return None
    team = await db.get(Team, team_id)
    if team is None or not team.account_id:
        return None
    row = await _load_row(db, email=normalized_email, team=team)
    if row is None:
        return None
    row.seat_type = seat_type
    row.seat_switch_count = (row.seat_switch_count or 0) + 1
    row.seat_switched_at = get_now()
    return row


async def record_quota_observation(
    db: AsyncSession,
    *,
    email: str,
    team_space_id: str,
    seat_type: str | None,
    usage: dict[str, Any],
) -> AccountPoolTeamUsage | None:
    """Record the latest quota check and preserve premium consumption history."""
    team = (await db.execute(select(Team).where(
        Team.account_id == team_space_id,
    ).order_by(Team.id.asc()))).scalars().first()
    if team is None:
        return None
    row = await _load_row(db, email=email.strip().lower(), team=team)
    if row is None:
        return None
    row.seat_type = seat_type or row.seat_type
    if usage["status"] != "ok":
        return row
    now = get_now()
    row.quota_checked_at = now
    row.short_reset_at = _reset_at(usage, "5h")
    row.weekly_reset_at = _reset_at(usage, "1week")
    weekly = (usage.get("1week") or {})
    weekly_used = weekly.get("used")
    weekly_remaining = weekly.get("remaining")
    if row.seat_type == "premium" and weekly_remaining is not None:
        consumed = weekly_used is not None and weekly_used > 0
        if consumed or weekly_remaining == 0:
            row.premium_used = True
            row.premium_used_at = row.premium_used_at or now
        elif row.premium_used is None:
            row.premium_used = False
    return row
