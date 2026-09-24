import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, Team, TeamEmailMapping
from app.services.account_pool import account_pool_service
from app.services.member_rotation import MemberRotationService


class MemberRotationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.team_service = SimpleNamespace(
            get_team_members=AsyncMock(return_value={
                "success": True,
                "members": [{
                    "user_id": "user-1",
                    "email": "old@example.com",
                    "status": "joined",
                    "role": "standard-user",
                    "seat_type": "premium",
                }],
            }),
            delete_team_member=AsyncMock(return_value={"success": True}),
            add_team_member=AsyncMock(return_value={"success": True, "status": "invited"}),
        )
        self.account_pool = SimpleNamespace(
            invite_replacement=AsyncMock(return_value={
                "success": True,
                "status": "invited",
                "email": "new@example.com",
            })
        )
        self.exporter = SimpleNamespace(complete=AsyncMock(return_value="exported"))
        self.service = MemberRotationService(
            self.team_service, self.account_pool, self.exporter
        )

    async def test_rotates_member_and_exports_replacement(self):
        result = await self.service.rotate(1, "user-1", "OLD@example.com", object())

        self.assertEqual(result["status"], "exported")
        self.assertTrue(result["completed"])
        self.team_service.delete_team_member.assert_awaited_once_with(
            1, "user-1", unittest.mock.ANY, email="old@example.com"
        )
        invite = self.account_pool.invite_replacement.await_args.kwargs["invite_member"]
        await invite(1, "new@example.com", object(), seat_type="premium")
        self.team_service.add_team_member.assert_awaited_once_with(
            1, "new@example.com", unittest.mock.ANY, seat_type="premium"
        )
        self.exporter.complete.assert_awaited_once_with(1, "new@example.com", unittest.mock.ANY)

    async def test_waits_for_invited_replacement(self):
        self.exporter.complete.return_value = "waiting"

        result = await self.service.rotate(1, "user-1", "old@example.com", object())

        self.assertEqual(result["status"], "waiting")
        self.assertFalse(result["completed"])
        self.assertIn("等待新成员", result["message"])

    async def test_owner_is_rejected_before_kick(self):
        self.team_service.get_team_members.return_value = {
            "success": True,
            "members": [{
                "user_id": "owner-1",
                "email": "owner@example.com",
                "status": "joined",
                "role": "account-owner",
            }],
        }

        result = await self.service.rotate(1, "owner-1", "owner@example.com", object())

        self.assertFalse(result["success"])
        self.assertIn("所有者", result["error"])
        self.team_service.delete_team_member.assert_not_awaited()

    async def test_kick_success_without_candidate_is_explicit_partial_result(self):
        self.account_pool.invite_replacement.return_value = {
            "success": True,
            "status": "no_candidate",
            "email": None,
        }

        result = await self.service.rotate(1, "user-1", "old@example.com", object())

        self.assertTrue(result["success"])
        self.assertFalse(result["completed"])
        self.assertEqual(result["status"], "replacement_failed")
        self.assertIn("已踢出", result["message"])

    async def test_real_pool_invitation_accepts_seat_type_callback(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as db:
                db.add(Team(id=1, email="owner@example.com", account_id="space",
                            access_token_encrypted="token"))
                db.add(AccountPoolEntry(email="new@example.com"))
                await db.commit()
                service = MemberRotationService(self.team_service, account_pool_service,
                                                self.exporter)
                result = await service.rotate(1, "user-1", "old@example.com", db)
                self.assertEqual(result["status"], "exported")
                self.team_service.add_team_member.assert_awaited_once_with(
                    1, "new@example.com", db, seat_type="premium")
                mapping = (await db.execute(select(TeamEmailMapping))).scalar_one()
                self.assertTrue(mapping.replacement_export_pending)
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
