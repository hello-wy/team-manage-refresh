"""Select an eligible account-pool candidate for a standard seat."""

from sqlalchemy import select

from app.models import (AccountPoolHistory, MemberAuthorization, QuotaSnapshot,
                        RotationAction, Sub2apiExportRecord)
from app.services.quota_rotation_policy import MAX_SNAPSHOT_AGE
from app.utils.time_utils import get_now


async def find_rotation_candidate(db, team, pool):
    excluded = set()
    while candidate := await pool.find_replacement_candidate(
        team.id, db, excluded_ids=frozenset(excluded)
    ):
        excluded.add(candidate.id)
        in_flight = (await db.execute(select(RotationAction.id).where(
            RotationAction.email == candidate.email,
            RotationAction.action_type == "invite",
            RotationAction.status.in_(("pending", "executing", "reconciling")),
        ).limit(1))).scalar_one_or_none()
        if in_flight:
            continue
        exported = (await db.execute(select(Sub2apiExportRecord.id).where(
            Sub2apiExportRecord.email == candidate.email,
            Sub2apiExportRecord.team_space_id == team.account_id,
        ).limit(1))).scalar_one_or_none()
        if exported:
            continue
        uncertain = (await db.execute(select(MemberAuthorization.id).where(
            MemberAuthorization.email == candidate.email,
            MemberAuthorization.account_id == team.account_id,
            MemberAuthorization.sub2api_import_uncertain.is_(True),
        ).limit(1))).scalar_one_or_none()
        if uncertain:
            continue
        history = (await db.execute(select(AccountPoolHistory.id).where(
            AccountPoolHistory.account_pool_id == candidate.id,
            AccountPoolHistory.team_id == team.id,
        ).limit(1))).scalar_one_or_none()
        if not history:
            return candidate
        snapshot = (await db.execute(select(QuotaSnapshot).where(
            QuotaSnapshot.email == candidate.email,
            QuotaSnapshot.team_space_id == team.account_id,
        ))).scalar_one_or_none()
        if (snapshot and snapshot.status == "ok" and snapshot.weekly_remaining
                and snapshot.observed_at >= get_now() - MAX_SNAPSHOT_AGE):
            return candidate
    return None
