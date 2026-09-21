"""Build account-pool rows from current memberships and historical joins."""
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolHistory, Team, TeamEmailMapping

ACTIVE_MAPPING_STATUSES = ("invited", "joined")


async def _active_mappings(db: AsyncSession, entries: list[AccountPoolEntry]):
    result = await db.execute(
        select(TeamEmailMapping, Team)
        .join(Team, Team.id == TeamEmailMapping.team_id)
        .where(
            TeamEmailMapping.email.in_([entry.email for entry in entries]),
            TeamEmailMapping.status.in_(ACTIVE_MAPPING_STATUSES),
        )
    )
    by_email = defaultdict(list)
    for mapping, team in result.all():
        by_email[mapping.email].append((mapping, team))
    return by_email


async def _entry_histories(db: AsyncSession, entries: list[AccountPoolEntry]):
    result = await db.execute(
        select(AccountPoolHistory)
        .where(AccountPoolHistory.account_pool_id.in_([entry.id for entry in entries]))
        .order_by(AccountPoolHistory.joined_at.desc())
    )
    by_entry = defaultdict(list)
    for history in result.scalars().all():
        by_entry[history.account_pool_id].append(history)
    return by_entry


def _row(entry: AccountPoolEntry, mappings, histories) -> dict[str, Any]:
    joined = [item for item in mappings if item[0].status == "joined"]
    invited = [item for item in mappings if item[0].status == "invited"]
    active = joined + invited
    team_options = []
    seen_team_ids = set()
    for mapping, team in active:
        if team.id in seen_team_ids:
            continue
        seen_team_ids.add(team.id)
        team_options.append({
            "id": team.id,
            "name": team.team_name,
            "email": team.email,
            "status": mapping.status,
        })
    status = "unassigned"
    if len({team.id for _, team in active}) > 1:
        status = "conflict"
    elif joined:
        status = "joined"
    elif invited:
        status = "invited"
    current = active[0] if len(team_options) == 1 else (None, None)
    seat_type = next((mapping.seat_type for mapping, _ in joined if mapping.seat_type), None)
    return {
        "id": entry.id,
        "email": entry.email,
        "seat_type": seat_type,
        "status": status,
        "team_id": current[1].id if current[1] else None,
        "team_name": current[1].team_name if current[1] else None,
        "team_email": current[1].email if current[1] else None,
        "team_options": team_options,
        "joined_at": max((history.joined_at for history in histories), default=None),
        "history_count": len(histories),
        "liveness_status": entry.liveness_status,
        "liveness_checked_at": entry.liveness_checked_at,
        "liveness_message": entry.liveness_message,
        "created_at": entry.created_at,
    }


async def build_pool_entry_data(
    db: AsyncSession, entries: list[AccountPoolEntry]
) -> list[dict[str, Any]]:
    if not entries:
        return []
    mappings = await _active_mappings(db, entries)
    histories = await _entry_histories(db, entries)
    return [
        _row(entry, mappings.get(entry.email, []), histories.get(entry.id, []))
        for entry in entries
    ]
