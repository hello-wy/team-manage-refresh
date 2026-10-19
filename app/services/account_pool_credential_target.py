"""Resolve saved pool accounts and Team owners for credential editing."""
from sqlalchemy import select

from app.models import AccountPoolEntry, Team


async def resolve_credential_entry(db, entry_id):
    if entry_id > 0:
        entry = await db.get(AccountPoolEntry, entry_id)
        return entry if entry is not None and entry.deleted_at is None else None
    team = await db.get(Team, -entry_id)
    if team is None or not team.email.strip():
        return None
    email = team.email.strip().lower()
    entry = (await db.execute(select(AccountPoolEntry).where(
        AccountPoolEntry.email == email,
    ))).scalar_one_or_none()
    return entry if entry is not None else AccountPoolEntry(email=email)
