import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.middleware.sessions import SessionMiddleware

from app.database import get_db
from app.dependencies.auth import require_admin
from app.routes import account_pool_team_checkout as route
from app.services.team_checkout import TeamCheckoutClient, TeamCheckoutError

URL = "https://checkout.stripe.com/c/pay/cs_test_team"
EXPORT = {"type": "sub2api-data", "accounts": [{"credentials": {"access_token": "test-token"}}]}


class TeamCheckoutClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        config = SimpleNamespace(team_checkout_base_url="https://pay.example.test",
                                 team_checkout_api_key=SecretStr("test-key"))
        factory = lambda **options: httpx.AsyncClient(transport=httpx.MockTransport(handler), **options)
        return TeamCheckoutClient(config, factory)

    async def test_posts_json_and_coupon_only_to_payment_server(self):
        def handler(request):
            import json
            self.assertEqual(str(request.url), "https://pay.example.test/api/integrations/team/checkout")
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            self.assertEqual(json.loads(request.content), {"account_json": EXPORT, "coupon": "PROMO"})
            return httpx.Response(200, json={"url": URL})
        self.assertEqual(await self.client(handler).create(EXPORT, "PROMO"), {"url": URL})

    async def test_api_errors_non_json_and_invalid_urls_surface(self):
        responses = [httpx.Response(401, json={"detail": "认证失败"}),
                     httpx.Response(502, text="<html>bad gateway</html>"),
                     httpx.Response(200, json={"url": "https://evil.test/pay"})]
        for response in responses:
            with self.subTest(status=response.status_code), self.assertRaises(TeamCheckoutError):
                await self.client(lambda request: response).create(EXPORT, "")


class TeamCheckoutRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key="test-session-key")
        self.app.include_router(route.router)
        self.db = AsyncMock()

        async def database():
            yield self.db
        self.app.dependency_overrides[get_db] = database
        self.client = TestClient(self.app)
        self.payment = SimpleNamespace(configuration=lambda: None, create=AsyncMock(return_value={"url": URL}))
        self.app.dependency_overrides[route.get_checkout_client] = lambda: self.payment

    def authorize(self):
        self.app.dependency_overrides[require_admin] = lambda: {"username": "admin"}

    def test_admin_required_before_login(self):
        with patch.object(route.admin, "_login_and_save_account_pool_entry", new=AsyncMock()) as login:
            self.assertEqual(self.client.post("/admin/account-pool/7/team-checkout", json={}).status_code, 401)
            login.assert_not_awaited()

    def test_logs_in_exports_on_server_and_returns_only_link(self):
        self.authorize()
        with patch.object(route.admin, "_login_and_save_account_pool_entry",
                          new=AsyncMock(return_value=SimpleNamespace(payload=EXPORT))) as login:
            response = self.client.post("/admin/account-pool/7/team-checkout", json={"workspace_id": "personal", "coupon": "PROMO"})
        self.assertEqual(response.json(), {"success": True, "url": URL})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(login.await_args.args[2].workspace_id, "personal")
        self.payment.create.assert_awaited_once_with(EXPORT, "PROMO")
        self.assertNotIn("test-token", response.text)

    def test_service_failure_is_explicit_and_not_retried(self):
        self.authorize()
        self.payment.create.side_effect = TeamCheckoutError("认证失败")
        with patch.object(route.admin, "_login_and_save_account_pool_entry",
                          new=AsyncMock(return_value=SimpleNamespace(payload=EXPORT))):
            response = self.client.post("/admin/account-pool/7/team-checkout", json={})
        self.assertEqual(response.status_code, 424)
        self.assertEqual(response.json()["error"], "认证失败")
        self.payment.create.assert_awaited_once()

    def test_missing_config_does_not_start_real_login(self):
        self.authorize()
        def missing():
            raise TeamCheckoutError("密钥未配置", status_code=503)
        self.payment.configuration = missing
        with patch.object(route.admin, "_login_and_save_account_pool_entry", new=AsyncMock()) as login:
            self.assertEqual(self.client.post("/admin/account-pool/7/team-checkout", json={}).status_code, 503)
            login.assert_not_awaited()
