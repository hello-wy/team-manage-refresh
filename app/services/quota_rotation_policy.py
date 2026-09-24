"""Deterministic FIFO quota rotation decisions."""

from dataclasses import dataclass
from datetime import timedelta

from app.utils.time_utils import get_now

MAX_SNAPSHOT_AGE = timedelta(minutes=5)
OWNER_ROLE = "account-owner"
ADMIN_ROLES = frozenset({"admin", "account-admin", OWNER_ROLE})


@dataclass(frozen=True)
class RotationDecision:
    email: str
    action: str
    reason: str


def quota_ready(mapping, snapshot):
    if snapshot is None or snapshot.status != "ok":
        return False
    if snapshot.observed_at < get_now() - MAX_SNAPSHOT_AGE:
        return False
    if snapshot.observed_seat_type != mapping.seat_type:
        return False
    return snapshot.weekly_remaining is not None


def next_action(members, snapshots, states, seat_balance):
    """Pick one confirmed action; unknown quota blocks only that member."""
    balance = seat_balance.get("balance") or {}
    if not seat_balance.get("success") or not all(
        balance.get(kind, {}).get("known") for kind in ("standard", "premium")
    ):
        return None
    ordered = sorted(members, key=lambda member: (
        states.get(member.email).queued_at if states.get(member.email)
        and states[member.email].queued_at else member.joined_at or member.created_at,
        member.member_role in ADMIN_ROLES,
        member.id,
    ))
    for member in ordered:
        snapshot = snapshots.get(member.email)
        if member.status != "joined" or not quota_ready(member, snapshot):
            continue
        state = states.get(member.email)
        if state and state.blocked_reason:
            continue
        if state and state.phase in {"wait_reset", "removed"}:
            continue
        if member.seat_type == "premium" and snapshot.weekly_remaining == 0:
            if member.member_role in ADMIN_ROLES or member.auto_kick_exempt:
                if balance["standard"]["remaining"] == 0:
                    continue
                return RotationDecision(member.email, "return_standard", "高级周额度耗尽，保留管理员")
            return RotationDecision(member.email, "remove", "高级周额度耗尽")
    premium_available = balance["premium"]["remaining"]
    if premium_available is None or premium_available <= 0:
        return None
    for member in ordered:
        snapshot = snapshots.get(member.email)
        if member.status != "joined" or member.seat_type != "standard":
            continue
        state = states.get(member.email)
        if state and state.blocked_reason:
            continue
        if state and state.phase in {"wait_reset", "removed"}:
            continue
        if quota_ready(member, snapshot) and snapshot.weekly_remaining == 0:
            return RotationDecision(member.email, "upgrade", "标准周额度耗尽，按 FIFO 使用高级席位")
    return None
