"""Shared quota and rotation views for admin pages and APIs."""

from sqlalchemy import select

from app.models import MemberAuthorization, QuotaSnapshot, RotationMemberState, Team, TeamEmailMapping
from app.services.quota_rotation_policy import next_action, quota_ready


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


async def load_team_rotation(db, team: Team, seat_balance=None):
    mappings = (await db.execute(select(TeamEmailMapping).where(
        TeamEmailMapping.team_id == team.id,
        TeamEmailMapping.status.in_(("joined", "invited")),
    ))).scalars().all()
    snapshots = (await db.execute(select(QuotaSnapshot).where(
        QuotaSnapshot.team_space_id == team.account_id,
        QuotaSnapshot.email.in_([row.email for row in mappings]),
    ))).scalars().all()
    states = (await db.execute(select(RotationMemberState).where(
        RotationMemberState.team_id == team.id,
    ))).scalars().all()
    authorizations = (await db.execute(select(MemberAuthorization).where(
        MemberAuthorization.team_id == team.id,
        MemberAuthorization.email.in_([row.email for row in mappings]),
    ))).scalars().all()
    by_email = {row.email: row for row in snapshots}
    by_state = {row.email: row for row in states}
    by_authorization = {row.email: row for row in authorizations}
    decision = next_action(mappings, by_email, by_state, seat_balance or {})
    rows = []
    for mapping in mappings:
        snapshot = by_email.get(mapping.email)
        state = by_state.get(mapping.email)
        rows.append({
            "email": mapping.email, "role": mapping.member_role or "member",
            "status": mapping.status, "seat_type": mapping.seat_type,
            "phase": state.phase if state else mapping.seat_type or "unknown",
            "queued_at": state.queued_at.isoformat() if state and state.queued_at else None,
            "blocked_reason": "SUB2API_IMPORT_UNCERTAIN" if by_authorization.get(mapping.email)
            and by_authorization[mapping.email].sub2api_import_uncertain
            else (state.blocked_reason if state else None),
            "quota_fresh": quota_ready(mapping, snapshot),
            "usage": snapshot_usage(snapshot),
        })
    return {"team_id": team.id, "team_name": team.team_name,
            "rotation_mode": team.rotation_mode, "members": rows,
            "seat_balance": seat_balance, "next_action": vars(decision) if decision else None}
