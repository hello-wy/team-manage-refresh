"""Persist seat switches and quota refresh state for account-pool Teams."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolTeamUsage, Team
from app.utils.time_utils import get_now


def _reset_at(usage: dict[str, Any] | None, key: str) -> str | None:
    return ((usage or {}).get(key) or {}).get("reset_at")


def _reset_timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()


def _reset_is_due(value: str | None, now: datetime) -> bool:
    reset_timestamp = _reset_timestamp(value)
    return reset_timestamp is not None and reset_timestamp <= now.timestamp()


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
    else:
        row.team_space_id = team.account_id
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
    team_id: int | None = None,
) -> AccountPoolTeamUsage | None:
    """Record the latest quota check and preserve premium consumption history."""
    team = await db.get(Team, team_id) if team_id is not None else None
    if team_id is None:
        team = (await db.execute(select(Team).where(
            Team.account_id == team_space_id,
        ).order_by(Team.id.asc()))).scalars().first()
    if team is None:
        return None
    if team.account_id != team_space_id:
        raise ValueError("额度工作区与 Team 不匹配")
    row = await _load_row(db, email=email.strip().lower(), team=team)
    if row is None:
        return None
    row.seat_type = seat_type or row.seat_type
    if usage["status"] != "ok":
        return row
    now = get_now()
    previous_weekly_reset_at = row.weekly_reset_at
    next_weekly_reset_at = _reset_at(usage, "1week")
    period_rolled = (
        previous_weekly_reset_at is not None
        and previous_weekly_reset_at != next_weekly_reset_at
        and _reset_is_due(previous_weekly_reset_at, datetime.now(timezone.utc))
    )
    row.quota_checked_at = now
    row.short_reset_at = _reset_at(usage, "5h")
    row.weekly_reset_at = next_weekly_reset_at
    if period_rolled:
        row.premium_used = None
        return row
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


async def reset_expired_premium_usage(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Clear premium usage flags whose weekly quota window has elapsed."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current_timestamp = current.timestamp()
    rows = (await db.execute(select(AccountPoolTeamUsage).where(
        AccountPoolTeamUsage.premium_used.is_not(None),
        AccountPoolTeamUsage.weekly_reset_at.is_not(None),
    ))).scalars().all()
    reset_count = 0
    for row in rows:
        reset_timestamp = _reset_timestamp(row.weekly_reset_at)
        if reset_timestamp is None or reset_timestamp > current_timestamp:
            continue
        row.premium_used = None
        reset_count += 1
    return reset_count
