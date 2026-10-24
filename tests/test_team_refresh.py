"""Account-pool relogin and encrypted Team credential recovery."""
import asyncio
import json
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, Team
from app.routes import api
from app.services.account_pool_credentials import account_pool_credential_service
from app.services.chatgpt import ChatGPTService
from app.services.encryption import encryption_service as cipher
from app.services.member_authorization import CLIENT_ID
from app.services.openai_automatic_login import OpenAIAutomaticLoginError
from app.services.team import TeamService
from app.services.team_refresh import TeamRefreshDependencies, TeamRefreshService
from app.utils.jwt_parser import JWTParser
from app.utils.time_utils import get_now

EMAIL = "owner@example.com"
ACCOUNT = "team-space"
SECRET = "JBSWY3DPEHPK3PXP"
TOKEN_LIFETIME_SECONDS = 3600
TEST_SIGNING_KEY = "unit-test-key-with-at-least-32-chars"


def login_tokens(*, email=EMAIL, account=ACCOUNT, expired=False):
    claims = {"email": email, "exp": int(time.time()) + (
        -TOKEN_LIFETIME_SECONDS if expired else TOKEN_LIFETIME_SECONDS),
        "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    access = jwt.encode(claims, TEST_SIGNING_KEY, algorithm="HS256")
    return {"success": True, "access_token": access, "id_token": access,
            "refresh_token": "new-refresh", "workspace": {"workspace_id": account}}


class TeamRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.team = Team(id=1, email=EMAIL, account_id=ACCOUNT, status="error",
                         access_token_encrypted=cipher.encrypt_token("old-at"),
                         refresh_token_encrypted=cipher.encrypt_token("old-rt"),
                         session_token_encrypted=cipher.encrypt_token("old-session"))
        self.entry = AccountPoolEntry(email=EMAIL,
            password_encrypted=cipher.encrypt_token("password"),
            two_factor_secret_encrypted=cipher.encrypt_token(SECRET))
        self.db.add_all([self.team, self.entry])
        await self.db.commit()
        self.remote = SimpleNamespace(
            create_oauth_authorize_url=ChatGPTService().create_oauth_authorize_url,
            clear_session=AsyncMock(),
            get_account_info=AsyncMock(return_value={"success": True,
                                                    "accounts": [{"account_id": ACCOUNT}]}))
        self.teams = SimpleNamespace(chatgpt_service=self.remote,
                                    sync_team_info=AsyncMock(side_effect=self.sync_ok))
        self.login = SimpleNamespace(login=AsyncMock(return_value=login_tokens()))
        self.service = TeamRefreshService(TeamRefreshDependencies(
            teams=self.teams, credentials=account_pool_credential_service,
            login=self.login, cipher=cipher, jwt=JWTParser()))

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def sync_ok(self, team_id, db, **options):
        self.team.status = "active"
        await db.commit()
        return {"success": True, "message": "同步成功"}

    async def set_status(self, status):
        self.team.status = status
        await self.db.commit()

    def assert_old_credentials(self):
        self.assertEqual(cipher.decrypt_token(self.team.access_token_encrypted), "old-at")
        self.assertEqual(cipher.decrypt_token(self.team.refresh_token_encrypted), "old-rt")
        self.assertEqual(cipher.decrypt_token(self.team.session_token_encrypted), "old-session")

    async def test_abnormal_account_relogs_once_and_persists_encrypted_credentials(self):
        result = await self.service.refresh(1, self.db)
        self.assertTrue(result["success"])
        self.assertTrue(result["relogin_attempted"])
        request = self.login.login.await_args.args[0]
        self.assertEqual((request.email, request.password, request.totp_secret),
                         (EMAIL, "password", SECRET))
        self.assertEqual(request.account_id, ACCOUNT)
        self.assertEqual(request.oauth_draft["client_id"], CLIENT_ID)
        await self.db.refresh(self.team)
        self.assertEqual(cipher.decrypt_token(self.team.access_token_encrypted),
                         self.login.login.return_value["access_token"])
        self.assertEqual(cipher.decrypt_token(self.team.id_token_encrypted),
                         self.login.login.return_value["id_token"])
        self.assertEqual(cipher.decrypt_token(self.team.refresh_token_encrypted), "new-refresh")
        self.assertIsNone(self.team.session_token_encrypted)
        self.assertEqual((self.team.status, self.team.client_id), ("active", CLIENT_ID))
        self.assertNotIn("new-refresh", self.team.refresh_token_encrypted)
        self.assertNotIn(SECRET, json.dumps(result))
        self.login.login.assert_awaited_once()
        self.remote.clear_session.assert_any_await(request.identifier)
        self.remote.clear_session.assert_any_await(EMAIL)

    async def test_healthy_full_and_subscription_expired_accounts_only_sync(self):
        for status in ("active", "full", "expired"):
            with self.subTest(status=status):
                await self.set_status(status)
                self.assertTrue((await self.service.refresh(1, self.db))["success"])
        self.login.login.assert_not_awaited()
        self.assert_old_credentials()

    async def test_refresh_discovers_invalid_credentials_and_then_relogs_once(self):
        await self.set_status("active")
        self.teams.sync_team_info.side_effect = [
            {"success": False, "error_code": "token_refresh_failed", "error": "expired"},
            {"success": True, "message": "同步成功"}]
        self.assertTrue((await self.service.refresh(1, self.db))["success"])
        self.assertEqual(self.teams.sync_team_info.await_count, 2)
        self.login.login.assert_awaited_once()

    async def test_banned_account_attempts_login_once(self):
        await self.set_status("banned")
        self.assertTrue((await self.service.refresh(1, self.db))["success"])
        self.login.login.assert_awaited_once()

    async def test_sync_marking_account_error_triggers_login(self):
        await self.set_status("active")
        async def sync(team_id, db):
            self.team.status = "error"
            await db.commit()
            return {"success": False, "error": "members failed"}
        self.teams.sync_team_info.side_effect = sync
        result = await self.service.refresh(1, self.db)
        self.assertFalse(result["success"])
        self.assertIn("复核失败", result["error"])
        self.login.login.assert_awaited_once()

    async def test_transient_sync_error_does_not_relog_healthy_account(self):
        await self.set_status("active")
        self.teams.sync_team_info.side_effect = None
        self.teams.sync_team_info.return_value = {"success": False, "error": "network failure"}
        self.assertFalse((await self.service.refresh(1, self.db))["success"])
        self.login.login.assert_not_awaited()

    async def test_missing_password_or_2fa_and_deleted_entries_return_visible_errors(self):
        for field, value, message in (("password_encrypted", None, "密码"),
                                      ("two_factor_secret_encrypted", None, "2FA"),
                                      ("deleted_at", get_now(), "密码")):
            original = getattr(self.entry, field)
            setattr(self.entry, field, value)
            await self.db.commit()
            result = await self.service.refresh(1, self.db)
            self.assertFalse(result["success"])
            self.assertIn(message, result["error"])
            self.assert_old_credentials()
            setattr(self.entry, field, original)
        self.login.login.assert_not_awaited()

    async def test_login_rejection_is_visible_and_does_not_overwrite_tokens(self):
        self.login.login.side_effect = OpenAIAutomaticLoginError("密码错误")
        result = await self.service.refresh(1, self.db)
        self.assertIn("密码错误", result["error"])
        self.assertFalse(result["success"])
        self.login.login.assert_awaited_once()
        self.remote.clear_session.assert_awaited_once_with("team-refresh-login-1")
        self.assert_old_credentials()

    async def test_invalid_identity_and_workspace_never_overwrite_credentials(self):
        cases = [login_tokens(email="other@example.com"), login_tokens(account="other-space"),
                 login_tokens(expired=True), {**login_tokens(), "refresh_token": ""},
                 {**login_tokens(), "workspace": {"workspace_id": "other-space"}},
                 {**login_tokens(), "id_token": login_tokens(email="other@example.com")["id_token"]}]
        for tokens in cases:
            with self.subTest(tokens=list(tokens)):
                self.login.login.return_value = tokens
                self.assertFalse((await self.service.refresh(1, self.db))["success"])
                self.assert_old_credentials()
        self.teams.sync_team_info.assert_not_awaited()

    async def test_missing_target_workspace_and_failed_probe_preserve_credentials(self):
        for probe in ({"success": True, "accounts": [{"account_id": "different"}]},
                      {"success": False, "error": "account deactivated"}):
            self.remote.get_account_info.return_value = probe
            self.assertFalse((await self.service.refresh(1, self.db))["success"])
            self.assert_old_credentials()

    async def test_recheck_failure_keeps_new_tokens_but_reports_failure_without_loop(self):
        self.teams.sync_team_info.side_effect = None
        self.teams.sync_team_info.return_value = {"success": False, "error": "still unavailable"}
        result = await self.service.refresh(1, self.db)
        self.assertFalse(result["success"])
        self.assertIn("still unavailable", result["error"])
        self.assertEqual(cipher.decrypt_token(self.team.refresh_token_encrypted), "new-refresh")
        self.login.login.assert_awaited_once()

    async def test_force_token_refresh_and_unknown_team_keep_existing_behavior(self):
        await self.service.refresh(1, self.db, force_refresh=True)
        self.teams.sync_team_info.assert_awaited_with(1, self.db, force_refresh=True)
        self.teams.sync_team_info.side_effect = None
        self.teams.sync_team_info.return_value = {"success": False, "error_code": "team_not_found"}
        result = await self.service.refresh(999, self.db)
        self.assertEqual(result["error_code"], "team_not_found")
        self.login.login.assert_not_awaited()

    async def test_parallel_refreshes_only_login_once_after_recovery(self):
        results = await asyncio.gather(self.service.refresh(1, self.db),
                                       self.service.refresh(1, self.db))
        self.assertTrue(all(result["success"] for result in results))
        self.login.login.assert_awaited_once()

    async def test_api_uses_recovery_service_and_preserves_failure_response(self):
        for success, code in ((True, 200), (False, 400)):
            result = {"success": success, "message": "done", "error": "failed"}
            refresh = AsyncMock(return_value=result)
            with patch.object(api.team_refresh_service, "refresh", refresh):
                response = await api.refresh_team(1, force=False, db=self.db, current_user={})
            self.assertEqual(response.status_code, code)
            self.assertEqual(json.loads(response.body), result)
            refresh.assert_awaited_once_with(1, self.db, force_refresh=False)

    async def test_real_sync_recovers_abnormal_status_and_preserves_full_and_expired(self):
        teams = TeamService()
        teams.ensure_access_token = AsyncMock(return_value=login_tokens()["access_token"])
        teams.chatgpt_service = self.remote
        self.remote.get_members = AsyncMock(return_value={"success": True, "members": [], "total": 0})
        self.remote.get_invites = AsyncMock(return_value={"success": True, "items": []})
        self.remote.get_seat_capacity = AsyncMock(return_value={"success": True, "data": {}})
        for before, members, expired, expected in (("error", 0, False, "active"),
                                                  ("banned", 6, False, "full"),
                                                  ("error", 0, True, "expired")):
            await self.set_status(before)
            expires = get_now() + timedelta(days=-1 if expired else 1)
            self.remote.get_account_info.return_value = {"success": True, "accounts": [{
                "account_id": ACCOUNT, "name": "Test", "plan_type": "team",
                "subscription_plan": "team", "expires_at": expires.isoformat()}]}
            self.remote.get_members.return_value["total"] = members
            self.assertTrue((await teams.sync_team_info(1, self.db))["success"])
            await self.db.refresh(self.team)
            self.assertEqual(self.team.status, expected)
