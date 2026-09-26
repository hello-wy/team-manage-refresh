"""Shared quota and rotation views for admin pages and APIs."""

from types import SimpleNamespace

from sqlalchemy import select

from app.models import (AccountPoolEntry, AccountPoolWorkspace, MemberAuthorization,
                        QuotaSnapshot, RotationMemberState, RotationSeatSnapshot,
                        Team, TeamEmailMapping)
from app.services.quota_rotation_policy import MAX_SNAPSHOT_AGE, next_action, quota_ready
from app.utils.time_utils import get_now


def snapshot_usage(snapshot):
    if snapshot is None:
        return {"status": "unknown", "error": "尚无后台额度快照"}
    values = {"status": snapshot.status, "error": snapshot.error,
              "observed_at": snapshot.observed_at.isoformat()}
    for key, prefix in (("5h", "short"), ("1week", "weekly")):
        remaining = getattr(snapshot, f"{prefix}_remaining")
        values[key] = {
            "remaining": remaining,
            "limit": getattr(snapshot, f"{prefix}_limit"),
            "used": None if remaining is None or getattr(snapshot, f"{prefix}_limit") is None
            else getattr(snapshot, f"{prefix}_limit") - remaining,
            "reset_at": getattr(snapshot, f"{prefix}_reset_at"),
            "state": "unknown" if remaining is None else
            ("exhausted" if remaining == 0 else "available"),
        }
    if snapshot.observed_seat_type == "premium":
        values["5h"] = {"state": "not_applicable"}
    return values


async def _stored_balance(db, team_id):
    seats = await db.get(RotationSeatSnapshot, team_id)
    if seats is None:
        return None
    fresh = seats.observed_at >= get_now() - MAX_SNAPSHOT_AGE
    return {"success": fresh, "observed_at": seats.observed_at.isoformat(),
            "error": None if fresh else "席位快照已过期",
            "balance": {
                "standard": {"known": True, "paid": seats.standard_paid,
                             "remaining": seats.standard_remaining},
                "premium": {"known": True, "paid": seats.premium_paid,
                            "remaining": seats.premium_remaining},
            }}


async def _login_and_authorizations(db, team, mappings):
    emails = [row.email for row in mappings]
    authorizations = (await db.execute(select(MemberAuthorization).where(
        MemberAuthorization.team_id == team.id,
        MemberAuthorization.account_id == team.account_id,
        MemberAuthorization.email.in_(emails),
    ))).scalars().all()
    entries = (await db.execute(select(AccountPoolEntry).where(
        AccountPoolEntry.email.in_(emails),
        AccountPoolEntry.deleted_at.is_(None),
    ))).scalars().all()
    workspaces = (await db.execute(select(AccountPoolWorkspace).where(
        AccountPoolWorkspace.account_pool_id.in_([row.id for row in entries]),
        AccountPoolWorkspace.workspace_id == team.account_id,
    ))).scalars().all()
    by_authorization = {row.email: row for row in authorizations}
    by_workspace = {row.account_pool_id: row for row in workspaces}
    login = {}
    for entry in entries:
        workspace = by_workspace.get(entry.id)
        login[entry.email] = bool(
            (workspace and workspace.export_json_encrypted)
            or (entry.workspace_id == team.account_id and entry.export_json_encrypted)
        )
    for email, authorization in by_authorization.items():
        login[email] = login.get(email, False) or bool(authorization.credentials_encrypted)
    return login, by_authorization


def _member_row(mapping, details):
    snapshot = details["snapshot"]
    state = details["state"]
    authorization = details["authorization"]
    return {
        "email": mapping.email, "role": mapping.member_role or "member",
        "status": mapping.status, "seat_type": mapping.seat_type,
        "has_login": details["has_login"],
        "phase": state.phase if state else mapping.seat_type or "unknown",
        "queued_at": state.queued_at.isoformat() if state and state.queued_at else None,
        "blocked_reason": "SUB2API_IMPORT_UNCERTAIN" if authorization
        and authorization.sub2api_import_uncertain
        else (state.blocked_reason if state else None),
        "quota_fresh": quota_ready(mapping, snapshot),
        "usage": snapshot_usage(snapshot),
    }


async def load_team_rotation(db, team: Team, seat_balance=None):
    if seat_balance is None:
        seat_balance = await _stored_balance(db, team.id)
    mappings = (await db.execute(select(TeamEmailMapping).where(
        TeamEmailMapping.team_id == team.id,
        TeamEmailMapping.status.in_(("joined", "invited")),
    ))).scalars().all()
    owner_email = team.email.strip().lower()
    if owner_email and not any(row.email == owner_email for row in mappings):
        mappings.append(SimpleNamespace(
            id=0, email=owner_email, status="joined",
            member_role=team.account_role or "account-owner", seat_type="unknown",
            joined_at=None, created_at=team.created_at or get_now(),
            auto_kick_exempt=True,
        ))
    snapshots = (await db.execute(select(QuotaSnapshot).where(
        QuotaSnapshot.team_space_id == team.account_id,
        QuotaSnapshot.email.in_([row.email for row in mappings]),
    ))).scalars().all()
    states = (await db.execute(select(RotationMemberState).where(
        RotationMemberState.team_id == team.id,
    ))).scalars().all()
    login, by_authorization = await _login_and_authorizations(db, team, mappings)
    by_email = {row.email: row for row in snapshots}
    by_state = {row.email: row for row in states}
    decision = next_action(mappings, by_email, by_state, seat_balance or {})
    rows = [_member_row(mapping, {
        "snapshot": by_email.get(mapping.email),
        "state": by_state.get(mapping.email),
        "authorization": by_authorization.get(mapping.email),
        "has_login": login.get(mapping.email, False),
    }) for mapping in mappings]
    return {"team_id": team.id, "team_name": team.team_name,
            "rotation_mode": team.rotation_mode, "members": rows,
            "seat_balance": seat_balance, "next_action": vars(decision) if decision else None}
