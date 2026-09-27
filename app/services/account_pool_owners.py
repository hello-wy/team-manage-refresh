"""Owner identities are visible in the pool, but never replacement candidates."""
from sqlalchemy import func, select

from app.models import AccountPoolEntry, Team


def owner_email_exists(email_column=AccountPoolEntry.email):
    return select(Team.id).where(
        func.lower(func.trim(Team.email)) == func.lower(func.trim(email_column))
    ).exists()


async def owner_emails(db, emails):
    return set((await db.execute(select(func.lower(func.trim(Team.email))).where(
        func.lower(func.trim(Team.email)).in_([email.strip().lower() for email in emails])
    ))).scalars())


async def require_non_owner(db, entries):
    if await owner_emails(db, [entry.email for entry in entries]):
        raise ValueError("Team 所有者不能在账号池中加入其他 Team、删除或更换 2FA")
