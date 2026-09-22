import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app.database import get_db
from app.dependencies.auth import require_admin
from app.main import app
from app.routes import admin
from app.services.encryption import encryption_service
from app.services.sub2api import Sub2apiError, Sub2apiService, normalize_base_url


class Sub2apiServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_creates_openai_account_with_every_openai_group(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                self.assertEqual(request.url.params["platform"], "openai")
                return httpx.Response(200, json={"code": 0, "data": [
                    {"id": 1, "platform": "openai"}, {"id": 2, "platform": "openai"},
                    {"id": 3, "platform": "claude"},
                ]})
            return httpx.Response(200, json={"code": 0, "data": {"id": 42}})

        transport = httpx.MockTransport(handler)
        service = Sub2apiService(lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs))
        payload = {"accounts": [{"name": "member", "type": "oauth", "platform": "openai",
                                 "credentials": {"refresh_token": "secret"}}]}
        with patch("app.services.sub2api.settings_service.get_setting", new=AsyncMock(
                side_effect=["https://solidapi.top", encryption_service.encrypt_token("admin-secret"),
                             "all", "10", "off"])):
            result = await service.import_member(payload, object())
        self.assertEqual(result, {"account_id": 42, "group_count": 2})
        self.assertEqual(requests[1].url.path, "/api/v1/admin/accounts")
        self.assertEqual(requests[1].headers["x-api-key"], "admin-secret")
        sent = json.loads(requests[1].read())
        self.assertEqual(sent["credentials"]["refresh_token"], "secret")
        self.assertEqual(sent["group_ids"], [1, 2])
        self.assertEqual(sent["concurrency"], 10)
        self.assertNotIn("extra", sent)

    async def test_import_uses_only_selected_groups(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"code": 0, "data": [
                    {"id": 1, "name": "one", "platform": "openai"},
                    {"id": 2, "name": "two", "platform": "openai"},
                ]})
            return httpx.Response(200, json={"code": 0, "data": {"id": 42}})

        service = Sub2apiService(lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs))
        settings = ["https://solidapi.top", encryption_service.encrypt_token("key"), "selected", "[2]", "7", "session"]
        with patch("app.services.sub2api.settings_service.get_setting", new=AsyncMock(side_effect=settings)):
            result = await service.import_member({"accounts": [{"name": "member"}]}, object())
        self.assertEqual(result["group_count"], 1)
        sent = json.loads(requests[1].read())
        self.assertEqual(sent["group_ids"], [2])
        self.assertEqual(sent["concurrency"], 7)
        self.assertEqual(sent["extra"]["codex_fingerprint_mode"], "session")

    async def test_missing_selected_group_blocks_creation(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": [
                {"id": 1, "name": "one", "platform": "openai"},
            ]})

        service = Sub2apiService(lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs))
        settings = ["https://solidapi.top", encryption_service.encrypt_token("key"), "selected", "[2]"]
        with patch("app.services.sub2api.settings_service.get_setting", new=AsyncMock(side_effect=settings)):
            with self.assertRaisesRegex(Sub2apiError, "不存在或已停用"):
                await service.import_member({"accounts": [{"name": "member"}]}, object())
        self.assertEqual(len(requests), 1)

    async def test_no_groups_blocks_creation(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": []})

        service = Sub2apiService(lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs))
        with patch("app.services.sub2api.settings_service.get_setting", new=AsyncMock(
                side_effect=["https://solidapi.top", encryption_service.encrypt_token("key")])):
            with self.assertRaisesRegex(Sub2apiError, "没有可用"):
                await service.import_member({"accounts": [{"name": "member"}]}, object())
        self.assertEqual(len(requests), 1)

    def test_rejects_non_root_url(self):
        for value in ("file:///tmp/x", "https://host/path", "https://user:pass@host", "https://host?q=1"):
            with self.subTest(value=value), self.assertRaises(Sub2apiError):
                normalize_base_url(value)


class Sub2apiRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: AsyncMock()

    def tearDown(self):
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
                ) as mark:
                    response = self.client.post("/admin/teams/1/members/authorization/import-sub2api",
                                                json={"email": " MEMBER@example.com "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(export.await_args.args[1], "member@example.com")
        self.assertEqual(push.await_args.args[0], payload)
        self.assertEqual(mark.await_args.args[:3], (1, "member@example.com", 42))
        self.assertNotIn("secret", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

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
        self.assertEqual(encryption_service.decrypt_token(values["sub2api_api_key_encrypted"]), "top-secret")
        self.assertNotIn("top-secret", response.text)

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
