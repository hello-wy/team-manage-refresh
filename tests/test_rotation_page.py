import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (AccountPoolEntry, AccountPoolWorkspace, MemberAuthorization,
                        QuotaSnapshot, Team, TeamEmailMapping)
from app.routes.rotation import rotation_member_quota
from app.services.rotation_read_model import load_team_rotation


class RotationPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.db = sessions()
        self.team = Team(id=1, email="owner@example.com", account_id="space-1",
                         access_token_encrypted="token")
        self.db.add(self.team)
        self.db.add_all([
            TeamEmailMapping(team_id=1, email=email, status="joined",
                             seat_type="standard", member_role="standard-user")
            for email in ("auth@example.com", "pool@example.com", "none@example.com")
        ])
        self.db.add(MemberAuthorization(team_id=1, account_id="space-1",
                                        email="auth@example.com", credentials_encrypted="encrypted"))
        self.db.add(AccountPoolEntry(id=1, email="pool@example.com"))
        self.db.add(AccountPoolWorkspace(account_pool_id=1, workspace_id="space-1",
                                         status="joined", export_json_encrypted="encrypted"))
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def test_login_status_matches_current_workspace(self):
        view = await load_team_rotation(self.db, self.team)
        by_email = {member["email"]: member for member in view["members"]}
        self.assertTrue(by_email["auth@example.com"]["has_login"])
        self.assertTrue(by_email["pool@example.com"]["has_login"])
        self.assertFalse(by_email["none@example.com"]["has_login"])
        self.team.account_id = "other-space"
        await self.db.commit()
        view = await load_team_rotation(self.db, self.team)
        self.assertFalse(any(member["has_login"] for member in view["members"]))

    async def test_team_fragment_renders_chinese_and_shared_quota(self):
        from app.main import templates
        view = await load_team_rotation(self.db, self.team)
        html = templates.env.get_template("admin/rotation/_team.html").render(team=view)
        self.assertIn("普通成员", html)
        self.assertIn("有登录状态", html)
        self.assertIn("无登录状态", html)
        self.assertIn("quota-period-5h", html)
        self.assertIn("quota-period-7d", html)

    async def test_member_quota_refresh_updates_only_requested_snapshot(self):
        self.db.add(QuotaSnapshot(email="none@example.com", team_space_id="space-1",
                                  status="ok", short_remaining=9, weekly_remaining=8))
        await self.db.commit()
        usage = {"status": "ok", "5h": {"remaining": 40, "limit": 100,
                "reset_at": None}, "1week": {"remaining": 70, "limit": 100,
                "reset_at": None}}
        with patch("app.routes.rotation.account_pool_usage_service.check_email",
                   new=AsyncMock(return_value=usage)) as check:
            result = await rotation_member_quota(1, "AUTH@example.com", db=self.db, user={})
        check.assert_awaited_once_with(self.db, "auth@example.com", "space-1")
        snapshots = (await self.db.execute(select(QuotaSnapshot))).scalars().all()
        by_email = {snapshot.email: snapshot for snapshot in snapshots}
        self.assertEqual(by_email["auth@example.com"].short_remaining, 40)
        self.assertEqual(by_email["none@example.com"].short_remaining, 9)
        self.assertIn("quota-period-7d", result["html"])
        self.assertEqual(result["blocked_reason"], "-")


if __name__ == "__main__":
    unittest.main()
