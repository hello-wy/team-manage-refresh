"""Database-backed leases for workspace mutations and candidate claims."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from app.models import RotationLease
from app.utils.time_utils import get_now

LEASE_DURATION = timedelta(minutes=10)


async def acquire_lease(db, resource_key):
    now = get_now()
    holder = str(uuid4())
    changed = await db.execute(update(RotationLease).where(
        RotationLease.resource_key == resource_key, RotationLease.expires_at <= now,
    ).values(holder=holder, expires_at=now + LEASE_DURATION,
             version=RotationLease.version + 1))
    if changed.rowcount:
        await db.commit()
        return holder
    db.add(RotationLease(resource_key=resource_key, holder=holder,
                         expires_at=now + LEASE_DURATION))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    return holder


async def release_lease(db, resource_key, holder):
    await db.execute(delete(RotationLease).where(
        RotationLease.resource_key == resource_key, RotationLease.holder == holder))
    await db.commit()
