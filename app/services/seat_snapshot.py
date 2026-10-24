"""Persist the last observed per-type seat balance for list views and rotation."""
from sqlalchemy import select

from app.models import RotationSeatSnapshot
from app.utils.time_utils import get_now


def complete_balance(balance_result):
    balance = (balance_result or {}).get("balance") or {}
    if not (balance_result or {}).get("success"):
        return None
    if not all(balance.get(kind, {}).get("known") for kind in ("standard", "premium")):
        return None
    return balance


async def store_seat_snapshot(db, team_id, balance):
    snapshot = await db.get(RotationSeatSnapshot, team_id)
    if snapshot is None:
        snapshot = RotationSeatSnapshot(team_id=team_id)
        db.add(snapshot)
    snapshot.standard_paid = balance["standard"]["paid"]
    snapshot.standard_remaining = balance["standard"]["remaining"]
    snapshot.premium_paid = balance["premium"]["paid"]
    snapshot.premium_remaining = balance["premium"]["remaining"]
    snapshot.observed_at = get_now()
    return snapshot


async def load_seat_snapshots(db, team_ids):
    if not team_ids:
        return {}
    rows = await db.execute(select(RotationSeatSnapshot).where(RotationSeatSnapshot.team_id.in_(team_ids)))
    return {row.team_id: row for row in rows.scalars()}


async def attach_seat_snapshots(db, teams):
    snapshots = await load_seat_snapshots(db, [team["id"] for team in teams])
    for team in teams:
        snapshot = snapshots.get(team["id"])
        team["seat_snapshot"] = snapshot and {
            "standard_paid": snapshot.standard_paid,
            "standard_remaining": snapshot.standard_remaining,
            "premium_paid": snapshot.premium_paid,
            "premium_remaining": snapshot.premium_remaining,
            "observed_at": snapshot.observed_at,
        }
    return teams
