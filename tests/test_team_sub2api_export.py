"""Owner exports exercise real credential recovery, authorization, and persistence."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, MemberAuthorization, Team
from app.services.account_pool_credentials import account_pool_credential_service
from app.services.chatgpt import ChatGPTService
from app.services.encryption import encryption_service as cipher
from app.services.member_authorization import MemberAuthorizationError, MemberAuthorizationService
from app.services.openai_automatic_login import OpenAIAutomaticLoginError
from app.services.sub2api import Sub2apiImportUncertain
from app.services.team import TeamService
from app.services.team_refresh import TeamRefreshDependencies, TeamRefreshService
from app.services.team_sub2api_export import TeamSub2apiExportDependencies, TeamSub2apiExportService

EMAIL = "owner@example.com"
WORKSPACE = "target-space"
TOKEN_LIFETIME_SECONDS = 3600
SIGNING_KEY = "unit-test-key-with-at-least-32-chars"


def login_result():
    token = jwt.encode({"email": EMAIL, "exp": int(time.time()) + TOKEN_LIFETIME_SECONDS,
                        "https://api.openai.com/auth": {"chatgpt_account_id": WORKSPACE}},
                       SIGNING_KEY, algorithm="HS256")
    return {"success": True, "access_token": token, "id_token": token,
            "refresh_token": "recovered-refresh", "workspace": {"workspace_id": WORKSPACE}}


class TeamSub2apiExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.team = Team(id=1, email=EMAIL, account_id=WORKSPACE, status="active",
                         access_token_encrypted=cipher.encrypt_token("expired-access"))
        self.entry = AccountPoolEntry(email=EMAIL, password_encrypted=cipher.encrypt_token("password"),
                                      two_factor_secret_encrypted=cipher.encrypt_token("JBSWY3DPEHPK3PXP"))
        self.db.add_all([self.team, self.entry])
        await self.db.commit()
        self.remote = SimpleNamespace(
            create_oauth_authorize_url=ChatGPTService().create_oauth_authorize_url,
            clear_session=AsyncMock(),
            get_account_info=AsyncMock(return_value={"success": True,
                                                    "accounts": [{"account_id": WORKSPACE}]}),
            get_members=AsyncMock(return_value={"success": True, "total": 1, "members": [
                {"email": EMAIL, "role": "account-owner", "id": "owner-id"}]}),
            get_invites=AsyncMock(return_value={"success": True, "items": []}),
            get_seat_capacity=AsyncMock(return_value={"success": True, "data": {}}),
        )
        self.teams = TeamService()
        self.teams.chatgpt_service = self.remote
        self.teams.sync_team_info = AsyncMock(side_effect=self.sync_ok)
        self.teams.ensure_access_token = AsyncMock(side_effect=self.access_token)
        self.login = SimpleNamespace(login=AsyncMock(return_value=login_result()))
        self.recovery = TeamRefreshService(TeamRefreshDependencies(
            teams=self.teams, credentials=account_pool_credential_service, login=self.login,
            cipher=cipher, jwt=self.teams.jwt_parser))
        self.authorization = MemberAuthorizationService(
            self.teams, self.login, account_pool_credential_service)
        self.target = SimpleNamespace(import_member=AsyncMock(return_value={"account_id": 42, "group_count": 1}))
        self.records = SimpleNamespace(record_success=AsyncMock())
        self.service = TeamSub2apiExportService(TeamSub2apiExportDependencies(
            recovery=self.recovery, authorization=self.authorization,
            sub2api=self.target, records=self.records))

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def access_token(self, team, db):
        return cipher.decrypt_token(team.access_token_encrypted)

    async def sync_ok(self, team_id, db):
        self.team.status = "active"
        await db.commit()
        return {"success": True, "message": "同步成功"}

    async def test_missing_refresh_and_authorization_relogin_then_export_in_same_request(self):
        result = await self.service.push(1, self.db)
        self.assertTrue(result["relogin_attempted"])
        self.assertEqual(result["account_id"], 42)
        self.login.login.assert_awaited_once()
        self.target.import_member.assert_awaited_once()
        payload = self.target.import_member.await_args.args[0]
        credentials = payload["accounts"][0]["credentials"]
        self.assertEqual(credentials["refresh_token"], "recovered-refresh")
        self.assertEqual(credentials["chatgpt_account_id"], WORKSPACE)
        self.assertEqual(cipher.decrypt_token(self.team.refresh_token_encrypted), "recovered-refresh")
        record = (await self.db.execute(select(MemberAuthorization))).scalar_one()
        self.assertEqual(record.sub2api_account_id, 42)
        self.assertIsNotNone(record.sub2api_exported_at)
        self.assertNotIn("recovered-refresh", json.dumps(result))

    async def test_revoked_credentials_recover_then_export(self):
        self.team.refresh_token_encrypted = cipher.encrypt_token("revoked-refresh")
        await self.db.commit()
        self.teams.sync_team_info.side_effect = [
            {"success": False, "error_code": "token_refresh_failed", "error": "凭证失效"},
            {"success": True, "message": "同步成功"},
        ]
        result = await self.service.push(1, self.db)
        self.assertTrue(result["relogin_attempted"])
        self.target.import_member.assert_awaited_once()
        self.login.login.assert_awaited_once()

    async def test_failed_login_prevents_import_and_success_record(self):
        self.login.login.side_effect = OpenAIAutomaticLoginError("2FA 验证失败")
        with self.assertRaisesRegex(MemberAuthorizationError, "2FA 验证失败"):
            await self.service.push(1, self.db)
        self.target.import_member.assert_not_awaited()
        self.records.record_success.assert_not_awaited()
        self.assertIsNone(self.team.refresh_token_encrypted)

    async def test_missing_saved_password_is_explicit(self):
        self.entry.password_encrypted = None
        await self.db.commit()
        with self.assertRaisesRegex(MemberAuthorizationError, "未保存.*密码"):
            await self.service.push(1, self.db)
        self.login.login.assert_not_awaited()
        self.target.import_member.assert_not_awaited()

    async def test_uncertain_target_creation_is_not_retried_or_marked_successful(self):
        self.target.import_member.side_effect = Sub2apiImportUncertain("创建结果不确定")
        with self.assertRaisesRegex(Sub2apiImportUncertain, "创建结果不确定"):
            await self.service.push(1, self.db)
        self.target.import_member.assert_awaited_once()
        self.records.record_success.assert_not_awaited()
        record = (await self.db.execute(select(MemberAuthorization))).scalar_one()
        self.assertIsNone(record.sub2api_exported_at)

    async def test_unknown_team_does_not_login(self):
        with self.assertRaisesRegex(MemberAuthorizationError, "Team 不存在"):
            await self.service.push(99, self.db)
        self.login.login.assert_not_awaited()
