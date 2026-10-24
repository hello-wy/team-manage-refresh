"""Keep Team owners as real account-pool entries, never replacement candidates."""
from sqlalchemy import func, select

from app.models import AccountPoolEntry, Team, TeamEmailMapping


def owner_email_exists(email_column=AccountPoolEntry.email):
    return select(Team.id).where(
        func.lower(func.trim(Team.email)) == func.lower(func.trim(email_column))
    ).exists()


async def owner_emails(db, emails):
    return set((await db.execute(select(func.lower(func.trim(Team.email))).where(
        func.lower(func.trim(Team.email)).in_([email.strip().lower() for email in emails])
    ))).scalars())


async def ensure_owner_entry(db, email, *, name=None):
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None, False
    entry = (await db.execute(select(AccountPoolEntry).where(
        func.lower(func.trim(AccountPoolEntry.email)) == normalized,
    ))).scalar_one_or_none()
    changed = False
    if entry is None:
        entry = AccountPoolEntry(email=normalized)
        db.add(entry)
        changed = True
    elif entry.deleted_at is not None:
        entry.deleted_at = None
        changed = True
    if name and not entry.name:
        from app.services.account_pool_profile import account_name_key
        entry.name = str(name).strip()[:255] or None
        entry.name_key = account_name_key(entry.name)
        changed = True
    return entry, changed


async def ensure_team_owners_in_pool(db):
    owners = (await db.execute(select(Team.email).where(
        Team.email.is_not(None), func.trim(Team.email) != "",
    ))).scalars().all()
    emails = {str(email).strip().lower() for email in owners if str(email).strip()}
    if not emails:
        return 0
    profile_rows = (await db.execute(select(
        TeamEmailMapping.email, TeamEmailMapping.member_name,
    ).where(
        func.lower(func.trim(TeamEmailMapping.email)).in_(emails),
        TeamEmailMapping.member_name.is_not(None),
    ).order_by(TeamEmailMapping.updated_at.desc(), TeamEmailMapping.id.desc()))).all()
    names = {}
    for email, name in profile_rows:
        names.setdefault(str(email).strip().lower(), name)
    changed = 0
    for email in sorted(emails):
        _, did_change = await ensure_owner_entry(db, email, name=names.get(email))
        changed += int(did_change)
    if changed:
        await db.commit()
    return changed


async def require_non_owner(db, entries):
    if await owner_emails(db, [entry.email for entry in entries]):
        raise ValueError("Team 所有者不能在账号池中加入其他 Team、删除或更换 2FA")
