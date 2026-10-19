"""Refresh displayed quota windows and persist the same results for page reloads."""
from sqlalchemy import select

from app.models import AccountPoolEntry, AccountPoolTeamUsage
from app.services.account_pool_listing import team_usage_view
from app.services.quota_sync import store_quota
from app.services.rotation_read_model import snapshot_usage


def _quota_key(entry):
    return (entry["email"], entry.get("quota_workspace_id", entry.get("workspace_id")) or "")


async def _usage_details(db, entries):
    rows = (await db.execute(select(AccountPoolEntry.email, AccountPoolTeamUsage).join(
        AccountPoolEntry, AccountPoolEntry.id == AccountPoolTeamUsage.account_pool_id,
    ).where(AccountPoolEntry.email.in_({entry["email"] for entry in entries})))).all()
    return {(email, usage.team_space_id): team_usage_view(usage) for email, usage in rows}


async def refresh_pool_quotas(db, entries, *, usage_service):
    usages = await usage_service.check_many(db, [_quota_key(entry) for entry in entries])
    refreshed = []
    for entry in entries:
        email, space_id = _quota_key(entry)
        snapshot = await store_quota(
            db, email=email, space_id=space_id, seat_type=entry.get("seat_type"),
            team_id=entry.get("quota_team_id", entry.get("workspace_team_id")),
            usage=usages[(email, space_id)],
        )
        refreshed.append({**entry, "usage": snapshot_usage(snapshot)})
    await db.commit()
    details = await _usage_details(db, entries)
    return [{**entry, "team_usage": details.get(_quota_key(entry))} for entry in refreshed]
