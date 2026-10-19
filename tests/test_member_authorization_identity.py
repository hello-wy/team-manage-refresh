"""Authorization must resolve hidden emails from members observed in the live Team."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, Team, TeamEmailMapping
from app.services.account_pool_profile import account_name_key
from app.services.chatgpt import ChatGPTService
from app.services.member_authorization import MemberAuthorizationError, MemberAuthorizationService
from app.services.team import TeamService
from test_member_authorization import tokens

EMAIL = "member@example.com"
ACCOUNT = "team-account"
USER_ID = "member-user"
NAME = "Member Name"
TEAM_ID = 1
JOINED_AT = "2026-10-15T10:32:46Z"


class MemberAuthorizationIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.team = Team(id=TEAM_ID, email="owner@example.com", account_id=ACCOUNT,
                         access_token_encrypted="unused")
        self.db.add(self.team)
        await self.db.commit()
        self.raw_member = {"id": USER_ID, "email": None, "name": NAME,
                           "created_time": JOINED_AT, "role": "standard-user"}
        self.remote = SimpleNamespace(
            get_members=AsyncMock(return_value={
                "success": True, "members": [self.raw_member], "total": 1,
            }),
            get_invites=AsyncMock(return_value={"success": True, "items": []}),
            create_oauth_authorize_url=Mock(side_effect=ChatGPTService().create_oauth_authorize_url),
        )
        self.teams = TeamService()
        self.teams.chatgpt_service = self.remote
        self.teams.ensure_access_token = AsyncMock(return_value="owner-token")
        self.login = SimpleNamespace(login=AsyncMock(return_value=tokens()))
        self.credentials = SimpleNamespace(get_credentials=AsyncMock(return_value={
            "password": "test-password", "two_factor_secret": "JBSWY3DPEHPK3PXP",
        }))
        self.service = MemberAuthorizationService(self.teams, self.login, self.credentials)

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def save_mapping(self):
        self.db.add(TeamEmailMapping(team_id=TEAM_ID, email=EMAIL, status="joined",
                                    upstream_user_id=USER_ID))
        await self.db.commit()

    async def test_automatic_login_resolves_hidden_email_before_reading_credentials(self):
        await self.save_mapping()
        self.service.check = AsyncMock(return_value={"membership": "joined"})
        result = await self.service.automatic_login(TEAM_ID, EMAIL, self.db)
        self.assertEqual(result["membership"], "joined")
        self.credentials.get_credentials.assert_awaited_once_with(self.db, EMAIL)
        request = self.login.login.await_args.args[0]
        self.assertEqual((request.email, request.account_id), (EMAIL, ACCOUNT))
        self.assertIsNone(self.raw_member["email"])
        self.remote.get_invites.assert_not_awaited()

    async def test_manual_authorization_uses_the_same_live_user_mapping(self):
        await self.save_mapping()
        result = await self.service.authorize(TEAM_ID, EMAIL, self.db)
        self.assertEqual(result["email"], EMAIL)
        self.assertTrue(result["authorize_url"])

    async def test_hidden_email_resolves_through_unique_pool_name(self):
        self.db.add(AccountPoolEntry(email=EMAIL, name=NAME, name_key=account_name_key(NAME)))
        await self.db.commit()
        self.assertEqual(await self.service._membership(self.team, EMAIL, self.db), "joined")

    async def test_removed_live_user_cannot_login_using_stale_mapping(self):
        await self.save_mapping()
        self.remote.get_members.return_value = {"success": True, "members": [], "total": 0}
        with self.assertRaisesRegex(MemberAuthorizationError, "不在当前 Team"):
            await self.service.automatic_login(TEAM_ID, EMAIL, self.db)
        self.login.login.assert_not_awaited()
        self.credentials.get_credentials.assert_not_awaited()

    async def test_mapping_cannot_override_an_explicit_upstream_email(self):
        await self.save_mapping()
        self.remote.get_members.return_value = {
            "success": True, "members": [{**self.raw_member, "email": "other@example.com"}],
            "total": 1,
        }
        self.assertEqual(await self.service._membership(self.team, EMAIL, self.db), "absent")

    async def test_unresolved_hidden_email_does_not_establish_membership(self):
        self.assertEqual(await self.service._membership(self.team, EMAIL, self.db), "absent")

    async def test_failed_live_member_read_cannot_use_saved_mapping(self):
        await self.save_mapping()
        self.remote.get_members.return_value = {"success": False}
        with self.assertRaisesRegex(MemberAuthorizationError, "成员列表读取失败"):
            await self.service.automatic_login(TEAM_ID, EMAIL, self.db)
        self.login.login.assert_not_awaited()
