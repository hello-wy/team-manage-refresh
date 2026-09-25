import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, AccountPoolTeamUsage, QuotaSnapshot, Team
from app.services.account_pool_team_usage import record_seat_switch
from app.services.quota_sync import store_quota
from app.services.wham_usage import parse_usage


class AccountPoolTeamUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_seat_switch_and_quota_usage_are_scoped_to_team(self):
        async with self.sessions() as session:
            session.add_all([
                Team(id=1, email="owner@example.com", account_id="space-1",
                     access_token_encrypted="token"),
                AccountPoolEntry(id=1, email="member@example.com"),
            ])
            await session.commit()

            row = await record_seat_switch(
                session, team_id=1, email="MEMBER@example.com", seat_type="premium"
            )
            await session.commit()
            self.assertEqual(row.seat_switch_count, 1)
            self.assertIsNone(row.premium_used)

            await store_quota(
                session,
                email="member@example.com",
                space_id="space-1",
                seat_type="premium",
                usage={
                    "status": "ok",
                    "5h": {"remaining": 8, "limit": 10, "reset_at": "short"},
                    "1week": {"used": 2, "remaining": 8, "limit": 10,
                              "reset_at": "weekly"},
                },
            )
            await session.commit()
            tracked = (await session.execute(select(AccountPoolTeamUsage))).scalar_one()
            self.assertTrue(tracked.premium_used)
            self.assertEqual(tracked.weekly_reset_at, "weekly")
            self.assertIsNotNone(tracked.quota_checked_at)

            await store_quota(
                session,
                email="member@example.com",
                space_id="space-1",
                seat_type="premium",
                usage={"status": "unavailable", "error": "Token 无效"},
            )
            await session.commit()
            self.assertEqual(tracked.weekly_reset_at, "weekly")
            self.assertTrue(tracked.premium_used)

            await record_seat_switch(
                session, team_id=1, email="member@example.com", seat_type="standard"
            )
            await session.commit()
            tracked = (await session.execute(select(AccountPoolTeamUsage))).scalar_one()
            self.assertEqual(tracked.seat_type, "standard")
            self.assertEqual(tracked.seat_switch_count, 2)
            self.assertTrue(tracked.premium_used)

    async def test_unchecked_and_unused_premium_are_distinct_per_team(self):
        async with self.sessions() as session:
            session.add_all([
                Team(id=1, email="owner-1@example.com", account_id="space-1",
                     access_token_encrypted="token"),
                Team(id=2, email="owner-2@example.com", account_id="space-2",
                     access_token_encrypted="token"),
                AccountPoolEntry(id=1, email="member@example.com"),
            ])
            await session.commit()
            first = await record_seat_switch(
                session, team_id=1, email="member@example.com", seat_type="premium"
            )
            second = await record_seat_switch(
                session, team_id=2, email="member@example.com", seat_type="premium"
            )
            self.assertIsNone(first.premium_used)
            self.assertIsNone(second.premium_used)

            await store_quota(
                session, email="member@example.com", space_id="space-1",
                seat_type="premium", usage={"status": "ok", "1week": {
                    "used": 0, "remaining": 10, "limit": 10, "reset_at": "reset-1",
                }},
            )
            await session.commit()
            self.assertFalse(first.premium_used)
            self.assertIsNone(second.premium_used)
            self.assertEqual(first.weekly_reset_at, "reset-1")
            self.assertIsNone(second.weekly_reset_at)

    async def test_fractional_wham_usage_is_saved_and_marks_premium_used(self):
        async with self.sessions() as session:
            session.add_all([
                Team(id=1, email="owner@example.com", account_id="space-1",
                     access_token_encrypted="token"),
                AccountPoolEntry(id=1, email="member@example.com"),
            ])
            await session.commit()
            usage = parse_usage({"rate_limit": {"primary_window": {
                "limit_window_seconds": 604800, "used_percent": 0.5,
                "reset_at": 1791000000,
            }}})
            await store_quota(
                session, email="member@example.com", space_id="space-1",
                seat_type="premium", usage={"status": "ok", **usage},
            )
            await session.commit()

        async with self.sessions() as session:
            snapshot = (await session.execute(select(QuotaSnapshot))).scalar_one()
            tracked = (await session.execute(select(AccountPoolTeamUsage))).scalar_one()
            self.assertEqual(snapshot.weekly_remaining, 99.5)
            self.assertTrue(tracked.premium_used)
            self.assertEqual(tracked.weekly_reset_at, "1791000000")


if __name__ == "__main__":
    unittest.main()
