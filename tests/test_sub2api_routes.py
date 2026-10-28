import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.database import get_db
from app.dependencies.auth import require_admin
from app.main import app
from app.routes import admin
from app.services.encryption import encryption_service
from app.services.sub2api import Sub2apiError


class Sub2apiRouteTests(unittest.TestCase):
    def setUp(self):
        self.recovery = patch.object(admin.team_sub2api_export_service._deps.recovery,
                                     "refresh", new=AsyncMock(return_value={"success": True}))
        self.recovery.start()
        self.records = patch.object(admin.sub2api_export_record_service,
                                    "record_success", new=AsyncMock())
        self.records.start()
        self.client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: AsyncMock()

    def tearDown(self):
        self.recovery.stop()
        self.records.stop()
        app.dependency_overrides.clear()
        self.client.close()

    def test_import_requires_admin(self):
        response = self.client.post("/admin/teams/1/members/authorization/import-sub2api",
                                    json={"email": "member@example.com"})
        self.assertIn(response.status_code, (401, 403))

    def test_import_checks_member_and_does_not_echo_credentials(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        payload = {"accounts": [{"credentials": {"refresh_token": "secret"}}]}
        with patch.object(admin.member_authorization_service, "export", new=AsyncMock(return_value=payload)) as export:
            with patch.object(admin.sub2api_service, "import_member", new=AsyncMock(
                    return_value={"account_id": 42, "group_count": 2})) as push:
                with patch.object(
                    admin.member_authorization_service,
                    "mark_sub2api_exported",
                    new=AsyncMock(),
                ) as mark, patch.object(admin.sub2api_export_record_service,
                                        "record_success", new=AsyncMock()):
                    response = self.client.post("/admin/teams/1/members/authorization/import-sub2api",
                                                json={"email": " MEMBER@example.com "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(export.await_args.args[1], "member@example.com")
        self.assertEqual(push.await_args.args[0], payload)
        self.assertEqual(mark.await_args.args[:3], (1, "member@example.com", 42))
        self.assertNotIn("secret", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_team_push_uses_owner_authorization_without_echoing_credentials(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        team = type("Owner", (), {"email": "owner@example.com", "refresh_token_encrypted": "encrypted",
                                     "account_id": "space", "team_name": "Team"})()
        session = AsyncMock()
        session.get.return_value = team
        app.dependency_overrides[get_db] = lambda: session
        payload = {"accounts": [{"credentials": {"refresh_token": "secret"}}]}
        with patch.object(admin.member_authorization_service, "export", new=AsyncMock(return_value=payload)) as export:
            with patch.object(admin.sub2api_service, "import_member", new=AsyncMock(
                    return_value={"account_id": 42, "group_count": 2})):
                with patch.object(admin.member_authorization_service, "mark_sub2api_exported", new=AsyncMock()) as mark:
                    response = self.client.post("/admin/teams/1/push-sub2api")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(export.await_args.args[:2], (1, "owner@example.com"))
        self.assertEqual(mark.await_args.args[:3], (1, "owner@example.com", 42))
        self.assertNotIn("secret", response.text)

    def test_team_push_exposes_relogin_failure_without_creating_account(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        session = AsyncMock()
        session.get.return_value = type("Owner", (), {"account_id": "space"})()
        app.dependency_overrides[get_db] = lambda: session
        recovery = admin.team_sub2api_export_service._deps.recovery.refresh
        recovery.return_value = {"success": False, "error": "异常账号重新登录失败：密码错误"}
        with patch.object(admin.sub2api_service, "import_member", new=AsyncMock()) as push:
            response = self.client.post("/admin/teams/1/push-sub2api")
        self.assertEqual(response.status_code, 400)
        self.assertIn("密码错误", response.text)
        push.assert_not_awaited()

    def test_batch_team_push_reports_each_failure_and_continues(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        session = AsyncMock()
        app.dependency_overrides[get_db] = lambda: session
        imported = {"team_id": 1, "email": "owner@example.com", "account_id": 42, "group_count": 2}
        with patch.object(admin, "_push_team_owner_to_sub2api", new=AsyncMock(
                side_effect=[imported, Sub2apiError("分组不存在")])) as push:
            response = self.client.post("/admin/teams/batch-push-sub2api", json={"ids": [1, 2]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["success_count"], response.json()["failed_count"]), (1, 1))
        self.assertEqual(response.json()["results"][1]["error"], "分组不存在")
        self.assertEqual(push.await_count, 2)
        session.rollback.assert_awaited_once()

    def test_batch_team_push_all_failures_is_not_success(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        with patch.object(admin, "_push_team_owner_to_sub2api", new=AsyncMock(
                side_effect=Sub2apiError("分组不存在"))):
            response = self.client.post("/admin/teams/batch-push-sub2api", json={"ids": [1]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])
        self.assertIn("分组不存在", response.json()["error"])

    def test_settings_store_encrypted_key_without_echo(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        with patch.object(admin.settings_service, "get_setting", new=AsyncMock(return_value="")):
            with patch.object(admin.settings_service, "update_settings", new=AsyncMock(return_value=True)) as save:
                response = self.client.post("/admin/settings/sub2api", json={
                    "base_url": "https://solidapi.top/", "api_key": "top-secret",
                    "group_mode": "selected", "group_ids": [3, 2, 3]})
        self.assertEqual(response.status_code, 200)
        values = save.await_args.args[1]
        self.assertEqual(values["sub2api_base_url"], "https://solidapi.top")
        self.assertEqual(values["sub2api_group_mode"], "selected")
        self.assertEqual(json.loads(values["sub2api_group_ids"]), [2, 3])
        self.assertEqual(values["sub2api_default_concurrency"], "10")
        self.assertEqual(values["sub2api_codex_fingerprint_mode"], "off")
        self.assertEqual(values["sub2api_excel_bps_enabled"], "false")
        self.assertEqual(encryption_service.decrypt_token(values["sub2api_api_key_encrypted"]), "top-secret")
        self.assertNotIn("top-secret", response.text)

    def test_settings_save_and_return_excel_bps_boolean(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        for enabled in (True, False):
            with self.subTest(enabled=enabled), patch.object(
                admin.settings_service, "get_setting", new=AsyncMock(return_value="encrypted")
            ), patch.object(admin.settings_service, "update_settings", new=AsyncMock(return_value=True)) as save:
                response = self.client.post("/admin/settings/sub2api", json={
                    "base_url": "https://solidapi.top", "excel_bps_enabled": enabled,
                })
                self.assertEqual(response.status_code, 200)
                self.assertIs(response.json()["excel_bps_enabled"], enabled)
                self.assertEqual(save.await_args.args[1]["sub2api_excel_bps_enabled"],
                                 "true" if enabled else "false")

    def test_selected_mode_requires_group(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        with patch.object(admin.settings_service, "get_setting", new=AsyncMock(return_value="encrypted")):
            response = self.client.post("/admin/settings/sub2api", json={
                "base_url": "https://solidapi.top", "api_key": "", "group_mode": "selected", "group_ids": []})
        self.assertEqual(response.status_code, 400)
        self.assertIn("至少选择", response.text)

    def test_groups_endpoint_returns_sanitized_openai_groups(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        groups = [{"id": 2, "name": "OpenAI 主分组", "platform": "openai", "secret": "hidden"}]
        with patch.object(admin.sub2api_service, "list_openai_groups", new=AsyncMock(return_value=groups)):
            response = self.client.post("/admin/settings/sub2api/groups", json={
                "base_url": "https://solidapi.top", "api_key": "top-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["groups"], [
            {"id": 2, "name": "OpenAI 主分组", "platform": "openai"}
        ])
        self.assertNotIn("top-secret", response.text)
        self.assertNotIn("hidden", response.text)
