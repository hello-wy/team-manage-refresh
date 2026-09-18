import unittest
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolHistory, Team, TeamEmailMapping
from app.services.account_pool import AccountPoolService
from app.services.team import TeamService
from app.utils.time_utils import get_now


class AccountPoolServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.service = AccountPoolService()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_batch_add_normalizes_and_deduplicates_emails(self):
        async with self.sessions() as session:
            result = await self.service.add_emails(
                session,
                content=" Alice@Example.com\nalice@example.com\ninvalid\n",
            )
            self.assertEqual(result["added"], ["alice@example.com"])
            self.assertEqual(result["invalid"], ["invalid"])

            repeat = await self.service.add_emails(session, emails=["ALICE@example.com"])
            self.assertTrue(repeat["success"])
            self.assertEqual(repeat["existing"], ["alice@example.com"])

    async def test_join_and_leave_creates_reopenable_history(self):
        async with self.sessions() as session:
            team = Team(
                id=1,
                email="owner@example.com",
                team_name="Alpha",
                account_id="account-1",
                access_token_encrypted="token",
                status="active",
                max_members=6,
                current_members=1,
            )
            session.add(team)
            await session.commit()
            await self.service.add_emails(session, emails=["member@example.com"])

            joined_at = get_now() - timedelta(hours=1)
            team_service = TeamService.__new__(TeamService)
            members = {"member@example.com": {"id": "user-1", "created_time": joined_at.isoformat()}}
            await team_service._reconcile_team_email_mappings(
                1, set(members), set(), session, joined_members=members
            )
            await session.commit()
            histories = (await session.execute(select(AccountPoolHistory))).scalars().all()
            self.assertEqual(len(histories), 1)
            self.assertIsNone(histories[0].left_at)

            mapping = (await session.execute(select(TeamEmailMapping))).scalar_one()
            mapping.missing_sync_count = 3
            await team_service._reconcile_team_email_mappings(1, set(), set(), session)
            await session.commit()
            history = (await session.execute(select(AccountPoolHistory))).scalar_one()
            self.assertIsNotNone(history.left_at)

    async def test_current_membership_has_one_team_priority(self):
        async with self.sessions() as session:
            teams = [
                Team(id=1, email="owner-1@example.com", team_name="Alpha", status="active", max_members=6, access_token_encrypted="token-1"),
                Team(id=2, email="owner-2@example.com", team_name="Beta", status="active", max_members=6, access_token_encrypted="token-2"),
            ]
            session.add_all(teams)
            await session.commit()
            await self.service.add_emails(session, emails=["member@example.com"])
            session.add_all([
                TeamEmailMapping(team_id=1, email="member@example.com", status="joined"),
                TeamEmailMapping(team_id=2, email="member@example.com", status="invited"),
            ])
            await session.commit()
            listing = await self.service.list_entries(session)
            self.assertEqual(listing["entries"][0]["status"], "conflict")
            self.assertEqual(listing["entries"][0]["team_id"], 1)


class AccountPoolInputTests(unittest.TestCase):
    def test_parse_emails_preserves_input_order(self):
        normalized, invalid = AccountPoolService.parse_emails(
            content="B@example.com\na@example.com\nB@example.com\n"
        )
        self.assertEqual(normalized, ["b@example.com", "a@example.com"])
        self.assertEqual(invalid, [])


if __name__ == "__main__":
    unittest.main()
