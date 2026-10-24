"""Persist the last observed per-type seat balance for list views and rotation."""
import asyncio
from weakref import WeakValueDictionary

from sqlalchemy import select

from app.models import RotationSeatSnapshot, Team
from app.utils.seats import normalize_seat_type
from app.utils.time_utils import get_now


SEAT_SNAPSHOT_TTL_SECONDS = 300
_refresh_locks = WeakValueDictionary()


def snapshot_is_fresh(snapshot):
    return bool(snapshot and snapshot.observed_at and
                0 <= (get_now() - snapshot.observed_at).total_seconds() < SEAT_SNAPSHOT_TTL_SECONDS)


async def refresh_list_seat_balance(team_service, team_id, db):
    # Coalesce concurrent visits without holding the seat-operation lock recursively.
    lock = _refresh_locks.setdefault(team_id, asyncio.Lock())
    async with lock:
        team = await db.get(Team, team_id)
        if team is None:
            return {"success": False, "error": "Team 不存在"}
        snapshot = await db.get(RotationSeatSnapshot, team_id, populate_existing=True)
        if snapshot_is_fresh(snapshot):
            balance = {kind: {"known": True, "paid": getattr(snapshot, f"{kind}_paid"),
                              "remaining": getattr(snapshot, f"{kind}_remaining")}
                       for kind in ("standard", "premium")}
        else:
            result = await team_service.get_team_seat_balance(team, db)
            balance = complete_balance(result)
            if balance is None:
                return {"success": False, "error": result.get("error") or "席位余额暂不可用，请稍后重试"}
            snapshot = await store_seat_snapshot(db, team_id, balance)
            await db.commit()
        return {"success": True, "joined_members": team.joined_members, "total_seats": team.total_seats,
                "seat_balance": {"success": True, "balance": balance,
                                 "observed_at": snapshot.observed_at.isoformat()},
                "observed_at": snapshot.observed_at.isoformat()}


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
        team["seat_balance_needs_refresh"] = not snapshot_is_fresh(snapshot)
        team["seat_snapshot"] = snapshot and {
            "standard_paid": snapshot.standard_paid,
            "standard_remaining": snapshot.standard_remaining,
            "premium_paid": snapshot.premium_paid,
            "premium_remaining": snapshot.premium_remaining,
            "observed_at": snapshot.observed_at,
        }
    return teams


def build_seat_layout(members, balance=None):
    """Show every purchased seat without treating unconfirmed capacity as available."""
    grouped = {kind: [] for kind in ("standard", "premium", "unknown")}
    for member in members:
        if member.get("status") not in {"joined", "invited"}:
            continue
        kind = normalize_seat_type(member.get("seat_type"))
        invited = member.get("status") == "invited"
        grouped[kind].append({
            "state": "invited" if invited else "joined",
            "label": member.get("email") or member.get("name") or "成员资料待同步",
            "detail": "邀请中" if invited else "所有者" if member.get("role") == "account-owner" else "已加入",
        })
    groups = []
    for kind, label, value in (("standard", "标准席位", "default"),
                               ("premium", "高级席位", "premium"),
                               ("unknown", "其他席位", "")):
        slots = sorted(grouped[kind], key=lambda item: item["state"] == "invited")
        if kind == "unknown" and not slots:
            continue
        quota = (balance or {}).get(kind, {})
        paid, remaining = quota.get("paid"), quota.get("remaining")
        known = (quota.get("known") and type(paid) is int and type(remaining) is int
                 and 0 <= remaining <= paid)
        if known:
            # A cached membership list can be newer than its capacity snapshot.
            remaining = min(remaining, max(0, paid - len(slots)))
            occupied = max(len(slots), paid - remaining)
            slots.extend({"state": "reserved", "label": "已占用 / 预留", "detail": "待同步确认"}
                         for _ in range(occupied - len(slots)))
            slots.extend({"state": "available", "label": "邀请账号", "detail": "空闲席位"}
                         for _ in range(remaining))
        groups.append({"kind": kind, "label": label, "seat_type": value,
                       "paid": paid if known else None, "remaining": remaining if known else None,
                       "slots": slots})
    return groups
