import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import RotationAction, Team
from app.services.quota_rotation import reconcile_action
from app.services.quota_rotation_policy import next_action
from app.utils.time_utils import get_now


def member(email="member@example.com", seat="standard", role="standard-user"):
    return SimpleNamespace(id=1, email=email, status="joined", seat_type=seat,
                           member_role=role, auto_kick_exempt=False,
                           joined_at=get_now(), created_at=get_now())


def snapshot(seat="standard", weekly=0, short=0):
    return SimpleNamespace(status="ok", observed_at=get_now(),
                           observed_seat_type=seat, weekly_remaining=weekly,
                           short_remaining=short)


def balance(standard=1, premium=1):
    return {"success": True, "balance": {
        "standard": {"known": True, "remaining": standard},
        "premium": {"known": True, "remaining": premium},
    }}


class RotationPolicyTests(unittest.TestCase):
    def test_short_window_exhaustion_does_not_rotate(self):
        row = member()
        self.assertIsNone(next_action([row], {row.email: snapshot(weekly=5)}, {}, balance()))

    def test_standard_weekly_exhaustion_only_upgrades(self):
        row = member()
        self.assertEqual(next_action([row], {row.email: snapshot()}, {}, balance()).action, "upgrade")

    def test_premium_weekly_exhaustion_removes_ordinary_member(self):
        row = member(seat="premium")
        self.assertEqual(next_action([row], {row.email: snapshot(seat="premium")}, {}, balance()).action, "remove")

    def test_admin_returns_only_when_standard_seat_is_free(self):
        row = member(seat="premium", role="admin")
        observed = {row.email: snapshot(seat="premium")}
        self.assertEqual(next_action([row], observed, {}, balance()).action, "return_standard")
        self.assertIsNone(next_action([row], observed, {}, balance(standard=0)))

    def test_unknown_or_old_seat_snapshot_blocks_member(self):
        row = member(seat="premium")
        old = snapshot(seat="standard")
        self.assertIsNone(next_action([row], {row.email: old}, {}, balance()))
        stale = snapshot(seat="premium")
        stale.observed_at = get_now() - timedelta(minutes=10)
        self.assertIsNone(next_action([row], {row.email: stale}, {}, balance()))

    def test_unknown_balance_pauses_team(self):
        row = member(seat="premium")
        seats = balance()
        seats["balance"]["standard"]["known"] = False
        self.assertIsNone(next_action([row], {row.email: snapshot(seat="premium")}, {}, seats))


class RotationReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_reconciles_without_repeating_remote_call(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as db:
                db.add(Team(id=1, email="owner@example.com", account_id="space",
                            access_token_encrypted="token", rotation_mode="auto"))
                action = RotationAction(team_id=1, email="member@example.com",
                                        action_type="upgrade", idempotency_key="1:member:1:upgrade",
                                        status="executing")
                db.add(action)
                await db.commit()
                service = SimpleNamespace(get_team_members=AsyncMock(return_value={
                    "success": True, "members": [{"email": "member@example.com",
                                                     "seat_type": "premium", "status": "joined"}],
                }), update_member_seat_type=AsyncMock())
                self.assertEqual(await reconcile_action(db, action, service), "succeeded")
                service.update_member_seat_type.assert_not_awaited()
        finally:
            await engine.dispose()
