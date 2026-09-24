"""Build account-pool rows from current memberships and historical joins."""
from collections import defaultdict
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolExportJob, AccountPoolHistory, AccountPoolWorkspace, Team, TeamEmailMapping

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


async def _saved_workspaces(db: AsyncSession, entries: list[AccountPoolEntry]):
    result = await db.execute(select(AccountPoolWorkspace).where(
        AccountPoolWorkspace.account_pool_id.in_([entry.id for entry in entries])
    ))
    by_entry = defaultdict(dict)
    for workspace in result.scalars().all():
        by_entry[workspace.account_pool_id][workspace.workspace_id] = workspace
    return by_entry


async def _latest_export_jobs(db: AsyncSession, entries: list[AccountPoolEntry]):
    result = await db.execute(select(AccountPoolExportJob).where(
        AccountPoolExportJob.account_pool_id.in_([entry.id for entry in entries])
    ).order_by(AccountPoolExportJob.id.desc()))
    jobs = {}
    for job in result.scalars().all():
        jobs.setdefault(job.account_pool_id, job)
    return jobs


async def _workspace_teams(db: AsyncSession, entries: list[AccountPoolEntry], saved):
    workspace_ids = {
        workspace_id for entry in entries
        for workspace_id in (entry.workspace_id, *saved[entry.id].keys()) if workspace_id
    }
    if not workspace_ids:
        return {}
    result = await db.execute(
        select(Team).where(Team.account_id.in_(workspace_ids)).order_by(Team.id.asc())
    )
    by_workspace = {}
    for team in result.scalars().all():
        by_workspace.setdefault(team.account_id, team)
    return by_workspace


def _team_options(mappings) -> list[dict[str, Any]]:
    options = []
    seen_team_ids = set()
    for mapping, team in mappings:
        if team.id in seen_team_ids:
            continue
        seen_team_ids.add(team.id)
        options.append({
            "id": team.id,
            "workspace_id": team.account_id,
            "name": team.team_name,
            "email": team.email,
            "status": mapping.status,
        })
    return options


def _workspace_options(entry, saved, teams) -> list[dict[str, Any]]:
    scan = json.loads(entry.workspace_state_json) if entry.workspace_state_json else {}
    discovered = scan.get("available_workspaces") or []
    if not discovered and entry.workspace_id:
        discovered = [{"id": entry.workspace_id, "name": entry.workspace_name,
                       "is_personal": entry.workspace_status == "personal_account"}]
    options = []
    for item in discovered:
        workspace_id = str(item.get("id") or "").strip()
        if not workspace_id:
            continue
        record = saved.get(workspace_id)
        team = teams.get(workspace_id)
        options.append({
            "id": workspace_id,
            "name": (team.team_name if team else None) or item.get("name") or workspace_id,
            "is_personal": bool(item.get("is_personal")),
            "status": record.status if record else "未获取 JSON",
            "json_saved": bool(record and record.export_json_encrypted),
            "json_updated_at": record.json_updated_at if record else None,
        })
    return options


def _workspace_data(entry: AccountPoolEntry, team: Team | None, saved, teams) -> dict[str, Any]:
    return {
        "workspace_id": entry.workspace_id,
        "workspace_name": entry.workspace_name,
        "workspace_status": entry.workspace_status,
        "workspace_checked_at": entry.workspace_checked_at,
        "workspace_team_id": team.id if team else None,
        "workspace_team_name": team.team_name if team else None,
        "workspace_team_email": team.email if team else None,
        "workspace_in_pool": team is not None,
        "workspace_is_personal": entry.workspace_status == "personal_account",
        "workspace_state_saved": bool(entry.workspace_state_json),
        "json_saved": bool(entry.export_json_encrypted),
        "json_updated_at": entry.export_json_updated_at,
        "workspace_options": _workspace_options(entry, saved, teams),
    }


def _row(entry: AccountPoolEntry, mappings, histories, workspace_team, saved, teams, job) -> dict[str, Any]:
    joined = [item for item in mappings if item[0].status == "joined"]
    invited = [item for item in mappings if item[0].status == "invited"]
    active = joined + invited
    team_options = _team_options(active)
    status = "unassigned"
    if len({team.id for _, team in active}) > 1:
        status = "conflict"
    elif joined:
        status = "joined"
    elif invited:
        status = "invited"
    current = active[0] if len(team_options) == 1 else (None, None)
    seat_type = next((mapping.seat_type for mapping, _ in joined if mapping.seat_type), None)
    row = {
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
        "export_job_id": job.id if job else None,
        "export_job_status": job.status if job else None,
        "export_job_error": job.error if job else None,
    }
    row.update(_workspace_data(entry, workspace_team, saved, teams))
    return row


async def build_pool_entry_data(
    db: AsyncSession, entries: list[AccountPoolEntry]
) -> list[dict[str, Any]]:
    if not entries:
        return []
    mappings = await _active_mappings(db, entries)
    histories = await _entry_histories(db, entries)
    saved = await _saved_workspaces(db, entries)
    jobs = await _latest_export_jobs(db, entries)
    workspace_teams = await _workspace_teams(db, entries, saved)
    return [
        _row(
            entry,
            mappings.get(entry.email, []),
            histories.get(entry.id, []),
            workspace_teams.get(entry.workspace_id),
            saved[entry.id], workspace_teams, jobs.get(entry.id),
        )
        for entry in entries
    ]
