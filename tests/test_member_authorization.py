import json
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.dependencies.auth import require_admin
from app.main import app
from app.models import MemberAuthorization, Team
from app.routes import admin
from app.services.account_pool_authorization import AccountPoolLoginResult
from app.services.chatgpt import ChatGPTService
from app.services.encryption import encryption_service
from app.services.member_authorization import MemberAuthorizationError, MemberAuthorizationService, REDIRECT_URI
from app.services.openai_automatic_login import OpenAIAutomaticLoginError
from app.services.team import TeamService
from app.utils.time_utils import get_now

EMAIL = "member@example.com"
ACCOUNT = "team-account"


def tokens(email=EMAIL, account=ACCOUNT, expired=False):
    claims = {"email": email, "exp": int(time.time()) + (-60 if expired else 3600),
              "https://api.openai.com/auth": {"chatgpt_account_id": account, "chatgpt_user_id": "member-user"}}
    token = jwt.encode(claims, "unit-test-key-with-at-least-32-chars", algorithm="HS256")
    return {"success": True, "access_token": token, "refresh_token": "test-member-refresh", "id_token": token}


class MemberAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = self.sessions()
        self.team = Team(id=1, email="owner@example.com", account_id=ACCOUNT,
                         access_token_encrypted="owner-encrypted", team_name="Test Team")
        self.db.add(self.team)
        await self.db.commit()
        self.remote = SimpleNamespace(
            create_oauth_authorize_url=Mock(side_effect=ChatGPTService().create_oauth_authorize_url),
            get_members=AsyncMock(return_value={"success": True, "members": [], "total": 0}),
            get_invites=AsyncMock(return_value={"success": True, "items": [{"email_address": EMAIL, "status": "pending"}]}),
            exchange_oauth_code=AsyncMock(return_value=tokens()),
            refresh_access_token_with_refresh_token=AsyncMock(return_value=tokens()),
            get_account_info=AsyncMock(return_value={"success": True, "accounts": [{"account_id": ACCOUNT}]}),
            get_seat_capacity=AsyncMock(return_value={"success": True, "data": {"seat_capacity": [
                {"type": "default", "paid": 2, "available": 1}, {"type": "prolite", "paid": 0, "available": 0},
            ]}}),
        )
        self.teams = TeamService()
        self.teams.chatgpt_service = self.remote
        self.teams.ensure_access_token = AsyncMock(return_value="owner-token")
        self.automatic_login = SimpleNamespace(login=AsyncMock(return_value=tokens()))
        self.credential_service = SimpleNamespace(get_credentials=AsyncMock(return_value={
            "email": EMAIL,
            "password": "member-password",
            "two_factor_secret": "JBSWY3DPEHPK3PXP",
        }))
        self.service = MemberAuthorizationService(
            self.teams, self.automatic_login, self.credential_service
        )

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def record(self):
        return (await self.db.execute(select(MemberAuthorization))).scalar_one()

    async def draft(self):
        result = await self.service.authorize(1, EMAIL, self.db)
        record = await self.record()
        self.assertNotIn("code_verifier", result)
        self.assertNotIn("access_token", result)
        return REDIRECT_URI + "?" + urlencode({"code": "test-code", "state": record.oauth_state})

    async def authorize(self):
        return await self.service.callback(1, EMAIL, await self.draft(), self.db)

    def join(self):
        self.remote.get_members.return_value = {"success": True, "members": [{"email": EMAIL, "id": "member-user"}], "total": 1}

    async def test_pending_authorization_is_encrypted_and_not_exportable(self):
        result = await self.authorize()
        self.assertTrue(result["authorized"])
        self.assertTrue(result["json_saved"])
        self.assertEqual(result["membership"], "invited")
        self.assertFalse(result["can_export"])
        record = await self.record()
        self.assertNotIn("test-member-refresh", record.credentials_encrypted)
        self.assertNotIn("test-member-refresh", record.export_json_encrypted)
        saved = json.loads(encryption_service.decrypt_token(record.export_json_encrypted))
        self.assertEqual(saved["accounts"][0]["credentials"]["email"], EMAIL)
        self.assertIsNone(record.oauth_state)
        self.assertNotIn("access_token", json.dumps(result))
        with self.assertRaisesRegex(MemberAuthorizationError, "等待"):
            await self.service.export(1, EMAIL, self.db)

    async def test_automatic_login_saves_same_json_and_refreshes_it_on_repeat(self):
        first = await self.service.automatic_login(1, EMAIL, self.db)
        self.assertTrue(first["authorized"])
        self.assertTrue(first["json_saved"])
        record = await self.record()
        first_updated_at = record.export_json_updated_at
        self.assertIn("test-member-refresh", encryption_service.decrypt_token(
            record.export_json_encrypted
        ))

        refreshed = {**tokens(), "refresh_token": "refreshed-member-token"}
        self.automatic_login.login.return_value = refreshed
        second = await self.service.automatic_login(1, EMAIL, self.db)
        saved = encryption_service.decrypt_token(record.export_json_encrypted)
        self.assertTrue(second["authorized"])
        self.assertIn("refreshed-member-token", saved)
        self.assertNotIn("test-member-refresh", saved)
        self.assertGreaterEqual(record.export_json_updated_at, first_updated_at)
        self.assertEqual(self.automatic_login.login.await_count, 2)
        self.assertEqual(len((await self.db.execute(select(MemberAuthorization))).scalars().all()), 1)

    async def test_automatic_login_requires_saved_password_and_totp(self):
        self.credential_service.get_credentials.return_value = {"password": "", "two_factor_secret": ""}
        with self.assertRaisesRegex(MemberAuthorizationError, "登录密码"):
            await self.service.automatic_login(1, EMAIL, self.db)
        self.credential_service.get_credentials.return_value = {
            "password": "member-password", "two_factor_secret": ""
        }
        with self.assertRaisesRegex(MemberAuthorizationError, "2FA"):
            await self.service.automatic_login(1, EMAIL, self.db)

    async def test_automatic_login_protocol_error_is_exposed_without_credentials(self):
        self.automatic_login.login.side_effect = OpenAIAutomaticLoginError("2FA 验证失败（HTTP 400）")
        with self.assertRaisesRegex(MemberAuthorizationError, "2FA 验证失败") as error:
            await self.service.automatic_login(1, EMAIL, self.db)
        self.assertNotIn("member-password", str(error.exception))

    async def test_joined_authorized_member_exports_native_sub2api_format(self):
        await self.authorize()
        self.join()
        payload = await self.service.export(1, EMAIL, self.db)
        self.assertEqual((payload["type"], payload["version"], payload["proxies"]), ("sub2api-data", 1, []))
        account = payload["accounts"][0]
        self.assertEqual(account["name"], EMAIL)
        self.assertEqual((account["platform"], account["type"]), ("openai", "oauth"))
        self.assertEqual(account["concurrency"], 10)
        self.assertEqual(account["credentials"]["chatgpt_account_id"], ACCOUNT)
        self.assertEqual(account["credentials"]["email"], EMAIL)
        self.assertEqual(account["credentials"]["chatgpt_user_id"], "member-user")
        self.assertTrue(account["credentials"]["expires_at"].endswith("Z"))
        self.assertNotIn("owner-token", json.dumps(payload))
        record = await self.record()
        saved = json.loads(encryption_service.decrypt_token(record.export_json_encrypted))
        self.assertEqual(saved["accounts"][0]["credentials"]["chatgpt_account_id"], ACCOUNT)

    async def test_sub2api_export_status_is_persisted_without_exposing_json(self):
        await self.authorize()
        await self.service.mark_sub2api_exported(1, EMAIL, 42, self.db)
        result = await self.service.check(1, EMAIL, self.db)
        self.assertTrue(result["sub2api_exported"])
        self.assertEqual(result["sub2api_account_id"], 42)
        self.assertIsNotNone(result["sub2api_exported_at"])
        self.assertNotIn("credentials", json.dumps(result))

    async def test_unauthed_joined_member_cannot_export(self):
        self.join()
        with self.assertRaisesRegex(MemberAuthorizationError, "尚未授权"):
            await self.service.export(1, EMAIL, self.db)

    async def test_team_owner_credentials_sync_into_member_authorization(self):
        owner_tokens = tokens(email="owner@example.com")
        self.team.access_token_encrypted = encryption_service.encrypt_token(owner_tokens["access_token"])
        self.team.refresh_token_encrypted = encryption_service.encrypt_token(owner_tokens["refresh_token"])
        self.team.id_token_encrypted = encryption_service.encrypt_token(owner_tokens["id_token"])
        self.team.client_id = "owner-client"
        self.teams.ensure_access_token.return_value = owner_tokens["access_token"]
        self.remote.get_members.return_value = {"success": True, "members": [{
            "email": "owner@example.com", "role": "account-owner", "id": "owner-user",
        }], "total": 1}

        snapshot = await self.teams.get_team_members(1, self.db)
        owner = snapshot["members"][0]
        self.assertTrue(owner["authorized"])
        self.assertTrue(owner["json_saved"])
        result = await self.service.check(1, "owner@example.com", self.db)
        self.assertTrue(result["can_export"])
        record = await self.record()
        saved = json.loads(encryption_service.decrypt_token(record.export_json_encrypted))
        self.assertEqual(saved["accounts"][0]["credentials"]["chatgpt_account_id"], ACCOUNT)
        self.assertNotIn("test-member-refresh", json.dumps(snapshot))
        self.assertEqual(len((await self.db.execute(select(MemberAuthorization))).scalars().all()), 1)

    async def test_owner_sync_rejects_mismatched_identity(self):
        wrong = tokens(email="other@example.com")
        self.team.refresh_token_encrypted = encryption_service.encrypt_token(wrong["refresh_token"])
        self.remote.get_members.return_value = {"success": True, "members": [{
            "email": "owner@example.com", "role": "account-owner",
        }], "total": 1}
        self.teams.ensure_access_token.return_value = wrong["access_token"]
        snapshot = await self.teams.get_team_members(1, self.db)
        self.assertFalse(snapshot["success"])
        self.assertIn("邮箱与 Team 所有者不一致", snapshot["error"])
        self.assertEqual((await self.db.execute(select(MemberAuthorization))).scalars().all(), [])

    async def test_missing_wrong_duplicate_state_and_other_callback_host_rejected(self):
        url = await self.draft()
        for bad in (REDIRECT_URI + "?code=x", REDIRECT_URI + "?state=wrong&code=x",
                    url + "&state=other", url.replace("localhost", "evil.example"), url + "#state=x"):
            with self.subTest(url=bad), self.assertRaises(MemberAuthorizationError):
                await self.service.callback(1, EMAIL, bad, self.db)
        self.remote.exchange_oauth_code.assert_not_awaited()

    async def test_replay_is_rejected(self):
        url = await self.draft()
        await self.service.callback(1, EMAIL, url, self.db)
        with self.assertRaisesRegex(MemberAuthorizationError, "已使用"):
            await self.service.callback(1, EMAIL, url, self.db)
        self.assertEqual(self.remote.exchange_oauth_code.await_count, 1)

    async def test_reopened_panel_can_resume_unexpired_authorization(self):
        await self.draft()
        result = await self.service.check(1, EMAIL, self.db)
        self.assertTrue(result["authorization_pending"])
        self.assertFalse(result["authorized"])
        self.assertNotIn("oauth_state", result)
        self.assertNotIn("verifier", json.dumps(result))

    async def test_join_check_returns_same_snapshot_and_persists_list_counts(self):
        await self.authorize()
        self.join()
        # 上游邀请列表尚未移除重复邮箱时，已加入状态优先。
        self.remote.get_members.reset_mock()
        result = await self.service.check(1, EMAIL, self.db)
        snapshot = result["members_snapshot"]
        self.assertEqual(result["membership"], "joined")
        self.assertTrue(snapshot["members"][0]["json_saved"])
        self.assertFalse(snapshot["members"][0]["sub2api_exported"])
        self.assertEqual(snapshot["members"][0]["status"], "joined")
        self.assertTrue(snapshot["members"][0]["authorized"])
        self.assertEqual(snapshot["seat_summary"]["invited"]["total"], 0)
        self.assertEqual((snapshot["joined_members"], snapshot["total_seats"]), (1, 2))
        self.assertEqual(self.remote.get_members.await_count, 1)
        async with self.sessions() as other:
            team = await other.get(Team, 1)
            self.assertEqual((team.joined_members, team.total_seats), (1, 2))
        self.assertNotIn("access_token", json.dumps(result))

    async def test_expired_link_rejected_before_exchange(self):
        url = await self.draft()
        (await self.record()).oauth_expires_at = get_now() - timedelta(seconds=1)
        await self.db.commit()
        with self.assertRaisesRegex(MemberAuthorizationError, "过期"):
            await self.service.callback(1, EMAIL, url, self.db)
        self.remote.exchange_oauth_code.assert_not_awaited()

    async def test_reissued_link_invalidates_previous_state(self):
        first = await self.draft()
        await self.draft()
        with self.assertRaisesRegex(MemberAuthorizationError, "state"):
            await self.service.callback(1, EMAIL, first, self.db)

    async def test_wrong_email_never_saved(self):
        self.remote.exchange_oauth_code.return_value = tokens("another@example.com")
        with self.assertRaisesRegex(MemberAuthorizationError, "邮箱"):
            await self.authorize()
        self.assertIsNone((await self.record()).credentials_encrypted)

    async def test_missing_refresh_token_rejected(self):
        self.remote.exchange_oauth_code.return_value = {**tokens(), "refresh_token": ""}
        with self.assertRaisesRegex(MemberAuthorizationError, "Refresh Token"):
            await self.authorize()
        self.assertIsNone((await self.record()).credentials_encrypted)

    async def test_failed_exchange_consumes_code_without_leaking_error(self):
        self.remote.exchange_oauth_code.return_value = {"success": False, "error": "secret authorization code"}
        with self.assertRaisesRegex(MemberAuthorizationError, "兑换失败") as error:
            await self.authorize()
        self.assertNotIn("secret", str(error.exception))
        self.assertIsNone((await self.record()).oauth_state)

    async def test_accessible_team_exports_when_token_default_workspace_is_personal(self):
        self.remote.exchange_oauth_code.return_value = tokens(account="personal-account")
        await self.authorize()
        self.join()
        payload = await self.service.export(1, EMAIL, self.db)
        credentials = payload["accounts"][0]["credentials"]
        self.assertEqual(credentials["chatgpt_account_id"], ACCOUNT)
        self.assertEqual(credentials["email"], EMAIL)

    async def test_removed_member_and_failed_reads_block_export(self):
        await self.authorize()
        self.remote.get_invites.return_value = {"success": True, "items": []}
        with self.assertRaisesRegex(MemberAuthorizationError, "不在当前 Team"):
            await self.service.export(1, EMAIL, self.db)
        self.remote.get_members.return_value = {"success": False}
        with self.assertRaisesRegex(MemberAuthorizationError, "读取失败"):
            await self.service.export(1, EMAIL, self.db)

    async def test_joined_member_exports_when_account_enumeration_omits_workspace(self):
        await self.authorize()
        self.join()
        self.remote.get_account_info.return_value = {"success": True, "accounts": []}
        payload = await self.service.export(1, EMAIL, self.db)
        self.assertEqual(payload["accounts"][0]["credentials"]["chatgpt_account_id"], ACCOUNT)
        self.remote.get_account_info.assert_not_awaited()

    async def test_target_email_must_be_in_team_or_pending_invites(self):
        with self.assertRaisesRegex(MemberAuthorizationError, "不在当前"):
            await self.service.authorize(1, "unrelated@example.com", self.db)
        self.remote.create_oauth_authorize_url.assert_not_called()

    async def test_team_account_switch_invalidates_authorization(self):
        await self.authorize()
        self.join()
        self.team.account_id = "another-team"
        await self.db.commit()
        with self.assertRaisesRegex(MemberAuthorizationError, "尚未授权"):
            await self.service.export(1, EMAIL, self.db)

    async def test_expired_access_refreshes_and_persists_rotated_credentials(self):
        await self.authorize()
        self.join()
        record = await self.record()
        record.credentials_encrypted = encryption_service.encrypt_token(json.dumps(tokens(expired=True)))
        await self.db.commit()
        self.remote.refresh_access_token_with_refresh_token.return_value = {**tokens(), "refresh_token": "rotated-refresh"}
        result = await self.service.export(1, EMAIL, self.db)
        self.assertEqual(result["accounts"][0]["credentials"]["refresh_token"], "rotated-refresh")
        self.assertIn("rotated-refresh", encryption_service.decrypt_token(record.credentials_encrypted))
        self.assertIn("rotated-refresh", encryption_service.decrypt_token(record.export_json_encrypted))

    async def test_failed_token_refresh_blocks_export(self):
        await self.authorize()
        self.join()
        (await self.record()).credentials_encrypted = encryption_service.encrypt_token(json.dumps(tokens(expired=True)))
        await self.db.commit()
        self.remote.refresh_access_token_with_refresh_token.return_value = {"success": False}
        with self.assertRaisesRegex(MemberAuthorizationError, "已失效"):
            await self.service.export(1, EMAIL, self.db)


class MemberAuthorizationRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        async def db():
            yield AsyncMock()
        app.dependency_overrides[get_db] = db

    def tearDown(self):
        app.dependency_overrides.clear()
        self.client.close()

    def test_all_endpoints_require_admin(self):
        for action in ("authorize", "automatic-login", "callback", "check", "export"):
            result = self.client.post(f"/admin/teams/1/members/authorization/{action}",
                                      json={"email": EMAIL, "callback_url": "test"})
            self.assertIn(result.status_code, (401, 403))

    def test_export_headers_and_no_credentials_in_denied_response(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        with patch.object(admin.member_authorization_service, "export", new=AsyncMock(side_effect=MemberAuthorizationError("尚未授权"))):
            response = self.client.post("/admin/teams/1/members/authorization/export", json={"email": EMAIL})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn("credentials", response.text)
        with patch.object(admin.member_authorization_service, "export", new=AsyncMock(return_value={"type": "sub2api-data", "accounts": []})):
            response = self.client.post("/admin/teams/1/members/authorization/export", json={"email": EMAIL})
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertIn(f'filename="sub2api-{EMAIL}.json"', response.headers["content-disposition"])
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_email_normalized_and_callback_payload_validated(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        with patch.object(admin.member_authorization_service, "check", new=AsyncMock(return_value={})) as check:
            response = self.client.post("/admin/teams/1/members/authorization/check", json={"email": " MEMBER@example.com "})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(check.await_args.args[1], EMAIL)
        self.assertEqual(self.client.post("/admin/teams/1/members/authorization/callback", json={"email": EMAIL}).status_code, 422)

    def test_account_pool_sub2api_export_always_logs_in_and_saves_json(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        payload = {"type": "sub2api-data", "accounts": []}
        result = AccountPoolLoginResult(
            payload=payload,
            workspace={"status": "workspace_ok", "workspace_id": "external-workspace"},
        )
        with patch.object(
            admin.account_pool_authorization_service,
            "login_entry",
            new=AsyncMock(return_value=result),
        ) as login, patch.object(
            admin.account_pool_authorization_service,
            "save_result",
            new=AsyncMock(return_value=True),
        ) as save, patch.object(
            admin.sub2api_service,
            "import_member",
            new=AsyncMock(return_value={"account_id": 42, "group_count": 2}),
        ) as push:
            response = self.client.post(
                "/admin/account-pool/7/automatic-login",
                json={"workspace_id": "external-workspace"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("attachment", response.headers.get("content-disposition", ""))
        self.assertEqual(response.json()["account_id"], 42)
        login.assert_awaited_once_with(
            unittest.mock.ANY,
            7,
            "external-workspace",
        )
        save.assert_awaited_once()
        push.assert_awaited_once_with(payload, unittest.mock.ANY)

    def test_account_pool_json_export_logs_in_saves_and_downloads(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        payload = {"type": "sub2api-data", "accounts": []}
        result = AccountPoolLoginResult(
            payload=payload,
            workspace={"status": "no_workspace", "workspace_id": ""},
        )
        with patch.object(
            admin.account_pool_authorization_service,
            "login_entry",
            new=AsyncMock(return_value=result),
        ) as login, patch.object(
            admin.account_pool_authorization_service,
            "save_result",
            new=AsyncMock(return_value=True),
        ) as save:
            response = self.client.post("/admin/account-pool/7/export-json", json={})

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(json.loads(response.content), payload)
        login.assert_awaited_once_with(unittest.mock.ANY, 7, "")
        save.assert_awaited_once()

    def test_account_pool_workspace_scan_persists_result(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        result = AccountPoolLoginResult(
            payload={"type": "sub2api-data", "accounts": []},
            workspace={"status": "workspace_ok", "workspace_id": "workspace-1"},
        )
        with patch.object(
            admin.account_pool_authorization_service,
            "login_entry",
            new=AsyncMock(return_value=result),
        ), patch.object(
            admin.account_pool_authorization_service,
            "save_result",
            new=AsyncMock(return_value=True),
        ) as save:
            response = self.client.post("/admin/account-pool/7/workspace-scan")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workspace"]["workspace_id"], "workspace-1")
        self.assertEqual(save.await_args.kwargs["liveness"][0], "alive")
