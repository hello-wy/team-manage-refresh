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
                side_effect=["https://solidapi.top", encryption_service.encrypt_token("admin-secret")])):
            result = await service.import_member(payload, object())
        self.assertEqual(result, {"account_id": 42, "group_count": 2})
        self.assertEqual(requests[1].url.path, "/api/v1/admin/accounts")
        self.assertEqual(requests[1].headers["x-api-key"], "admin-secret")
        sent = json.loads(requests[1].read())
        self.assertEqual(sent["credentials"]["refresh_token"], "secret")
        self.assertEqual(sent["group_ids"], [1, 2])

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
                response = self.client.post("/admin/teams/1/members/authorization/import-sub2api",
                                            json={"email": " MEMBER@example.com "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(export.await_args.args[1], "member@example.com")
        self.assertEqual(push.await_args.args[0], payload)
        self.assertNotIn("secret", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_settings_store_encrypted_key_without_echo(self):
        app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        with patch.object(admin.settings_service, "get_setting", new=AsyncMock(return_value="")):
            with patch.object(admin.settings_service, "update_settings", new=AsyncMock(return_value=True)) as save:
                response = self.client.post("/admin/settings/sub2api", json={
                    "base_url": "https://solidapi.top/", "api_key": "top-secret"})
        self.assertEqual(response.status_code, 200)
        values = save.await_args.args[1]
        self.assertEqual(values["sub2api_base_url"], "https://solidapi.top")
        self.assertEqual(encryption_service.decrypt_token(values["sub2api_api_key_encrypted"]), "top-secret")
        self.assertNotIn("top-secret", response.text)
