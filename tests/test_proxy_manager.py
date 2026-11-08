import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, get_db
from app.dependencies.auth import require_admin
from app.models import AccountPoolEntry, AccountProxyRoute, MemberAuthorization, ProxyEndpoint, Team
from app.routes import proxies
from app.services.chatgpt import ChatGPTService
from app.services.encryption import encryption_service
from app.services.proxy_manager import (
    GEO_CHECK_URL, IP_CHECK_URL, ProxyProbe, ProxyRoutingError, identifier_email,
    public_proxy, resolve_login_proxy, save_route, validate_managed_proxy,
)
from app.services.settings import settings_service

PROXY_URL = "socks5h://proxy-user:private-password@198.51.100.2:1080"


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class FakeSession:
    instances = []
    responses = []

    def __init__(self, **kwargs):
        self.kwargs, self.requests, self.closed = kwargs, [], False
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        self.closed = True

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response = type(self).responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings_service.clear_cache()
        FakeSession.instances, FakeSession.responses = [], []
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            self.account = AccountPoolEntry(email="member@example.com")
            self.other = AccountPoolEntry(email="other@example.com")
            self.team = Team(email="owner@example.com", access_token_encrypted="unused", account_id="workspace")
            self.proxy = ProxyEndpoint(name="美国静态出口", url_encrypted=encryption_service.encrypt_token(PROXY_URL))
            db.add_all([self.account, self.other, self.team, self.proxy])
            await db.flush()
            self.member = MemberAuthorization(team_id=self.team.id, email=self.account.email, account_id="workspace")
            db.add(self.member)
            await db.commit()

    async def asyncTearDown(self):
        settings_service.clear_cache()
        await self.engine.dispose()


class ProxyRoutingTests(DatabaseCase):
    async def test_account_override_default_and_explicit_direct(self):
        async with self.sessions() as db:
            await settings_service.update_proxy_config(db, True, "http://global.example:80")
            self.assertEqual(await resolve_login_proxy(db, self.account.email), "http://global.example:80")
            await save_route(db, "*", "proxy", self.proxy.id)
            self.assertEqual(await resolve_login_proxy(db, self.other.email), PROXY_URL)
            await save_route(db, self.account.email, "direct", None)
            self.assertIsNone(await resolve_login_proxy(db, self.account.email))
            await save_route(db, self.account.email, "inherit", None)
            self.assertEqual(await resolve_login_proxy(db, self.account.email), PROXY_URL)
            await save_route(db, "*", "direct", None)
            self.assertIsNone(await resolve_login_proxy(db, self.other.email))

    async def test_all_login_identity_keys_resolve_the_same_email(self):
        async with self.sessions() as db:
            await save_route(db, self.account.email, "proxy", self.proxy.id)
            for key in [f"account-pool-login-{self.account.id}", f"account-pool-liveness-{self.account.id}",
                        f"member-auto-login-{self.member.id}", f"member-oauth-{self.member.id}",
                        f"member-delete-{self.member.id}", "team-import-login-member@example.com", "MEMBER@EXAMPLE.COM"]:
                with self.subTest(key=key):
                    self.assertEqual(await resolve_login_proxy(db, key), PROXY_URL)
            await save_route(db, self.team.email, "proxy", self.proxy.id)
            self.assertEqual(await resolve_login_proxy(db, f"team-refresh-login-{self.team.id}"), PROXY_URL)
            self.assertEqual(await resolve_login_proxy(db, "acc_workspace"), PROXY_URL)

    async def test_disabled_assigned_proxy_never_falls_back(self):
        async with self.sessions() as db:
            await save_route(db, self.account.email, "proxy", self.proxy.id)
            proxy = await db.get(ProxyEndpoint, self.proxy.id)
            proxy.enabled = False
            await db.commit()
            with self.assertRaisesRegex(ProxyRoutingError, "已停用"):
                await resolve_login_proxy(db, self.account.email)
            with self.assertRaisesRegex(ValueError, "已启用"):
                await save_route(db, self.other.email, "proxy", self.proxy.id)

    async def test_unknown_login_id_is_blocked_instead_of_becoming_direct(self):
        async with self.sessions() as db:
            with self.assertRaises(ProxyRoutingError):
                await identifier_email(db, "account-pool-login-99999")

    async def test_routes_reject_unknown_accounts_and_inconsistent_modes(self):
        async with self.sessions() as db:
            with self.assertRaisesRegex(ValueError, "账号不存在"):
                await save_route(db, "not-imported@example.com", "proxy", self.proxy.id)
            with self.assertRaisesRegex(ValueError, "只有指定"):
                await save_route(db, self.account.email, "direct", self.proxy.id)

    async def test_session_reuses_proxy_and_replaces_connection_after_route_change(self):
        service = ChatGPTService()
        async with self.sessions() as db:
            with patch("app.services.chatgpt.AsyncSession", FakeSession):
                await save_route(db, self.account.email, "proxy", self.proxy.id)
                key = f"account-pool-login-{self.account.id}"
                first = await service._get_session(db, key)
                self.assertIs(await service._get_session(db, key), first)
                self.assertEqual(first.kwargs["proxies"]["all"], PROXY_URL)
                self.assertFalse(first.kwargs["trust_env"])
                await save_route(db, self.account.email, "direct", None)
                second = await service._get_session(db, key)
                self.assertIsNot(first, second)
                self.assertTrue(first.closed)
                self.assertIsNone(second.kwargs["proxies"])
                await service.close()

    async def test_explicit_email_routes_pending_import_and_persists_through_exchange(self):
        service = ChatGPTService()
        async with self.sessions() as db:
            with patch("app.services.chatgpt.AsyncSession", FakeSession):
                await save_route(db, self.account.email, "proxy", self.proxy.id)
                first = await service._get_session(db, "custom-login-key", email=self.account.email)
                self.assertEqual(first.kwargs["proxies"]["https"], PROXY_URL)
                self.assertIs(await service._get_session(db, "custom-login-key"), first)
                await service.close()

    async def test_member_token_email_takes_priority_over_workspace_owner(self):
        service = ChatGPTService()
        service.jwt_parser.extract_email = lambda token: self.account.email
        async with self.sessions() as db:
            with patch("app.services.chatgpt.AsyncSession", FakeSession):
                await save_route(db, self.account.email, "proxy", self.proxy.id)
                FakeSession.responses = [Response({"ok": True})]
                result = await service._make_request("GET", "https://chatgpt.com/test", {
                    "Authorization": "Bearer unused", "chatgpt-account-id": "workspace",
                }, db_session=db)
                self.assertTrue(result["success"])
                self.assertEqual(FakeSession.instances[0].kwargs["proxies"]["all"], PROXY_URL)
                self.assertIn(self.account.email, service._sessions)
                await service.close()

    async def test_changing_proxy_address_replaces_cached_connection(self):
        service = ChatGPTService()
        async with self.sessions() as db:
            with patch("app.services.chatgpt.AsyncSession", FakeSession):
                await save_route(db, self.account.email, "proxy", self.proxy.id)
                first = await service._get_session(db, self.account.email)
                proxy = await db.get(ProxyEndpoint, self.proxy.id)
                proxy.url_encrypted = encryption_service.encrypt_token("http://198.51.100.3:8080")
                await db.commit()
                second = await service._get_session(db, self.account.email)
                self.assertTrue(first.closed)
                self.assertEqual(second.kwargs["proxies"]["all"], "http://198.51.100.3:8080")
                await service.close()


class ProxyTransportTests(DatabaseCase):
    async def test_real_curl_transport_uses_authenticated_proxy_for_requests_and_probe(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, self.headers.get("Proxy-Authorization")))
                if "/geo/" in self.path:
                    payload = {"success": True, "country": "Test country", "city": "Test city"}
                else:
                    payload = {"ip": "198.51.100.77"}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        service = ChatGPTService()
        try:
            async with self.sessions() as db:
                proxy = await db.get(ProxyEndpoint, self.proxy.id)
                proxy.url_encrypted = encryption_service.encrypt_token(
                    f"http://qa-user:qa-secret@127.0.0.1:{server.server_port}"
                )
                await db.commit()
                await save_route(db, self.account.email, "proxy", proxy.id)
                response = await service._make_request(
                    "GET", "http://proxy-check.invalid/account", {}, db_session=db,
                    identifier=self.account.email,
                )
                self.assertTrue(response["success"])
                with patch("app.services.proxy_manager.IP_CHECK_URL", "http://proxy-check.invalid/ip"), patch(
                    "app.services.proxy_manager.GEO_CHECK_URL", "http://proxy-check.invalid/geo/"
                ):
                    result = await ProxyProbe().test(db, proxy.id)
                self.assertEqual(result["exit_ip"], "198.51.100.77")
                self.assertEqual(result["test_status"], "online")
                self.assertEqual(result["country"], "Test country")
                self.assertNotIn("qa-secret", json.dumps(result))
            self.assertEqual([path for path, auth in received], [
                "http://proxy-check.invalid/account", "http://proxy-check.invalid/ip",
                "http://proxy-check.invalid/geo/198.51.100.77",
            ])
            self.assertTrue(all(auth == "Basic cWEtdXNlcjpxYS1zZWNyZXQ=" for _, auth in received))
        finally:
            await service.close()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)


class ProxyProbeTests(DatabaseCase):
    async def test_probe_records_observed_ip_geography_and_hides_credentials(self):
        FakeSession.responses = [Response({"ip": "198.51.100.7"}), Response({
            "success": True, "country": "美国", "region": "California", "city": "Los Angeles",
        })]
        async with self.sessions() as db:
            result = await ProxyProbe(FakeSession).test(db, self.proxy.id)
        self.assertEqual(result["test_status"], "online")
        self.assertEqual(result["exit_ip"], "198.51.100.7")
        self.assertEqual(result["country"], "美国")
        self.assertGreaterEqual(result["latency_ms"], 1)
        self.assertIsNotNone(result["tested_at"])
        self.assertNotIn("private-password", json.dumps(result))
        self.assertNotIn("proxy-user", result["address"])
        client = FakeSession.instances[0]
        self.assertEqual(client.kwargs["proxies"]["https"], PROXY_URL)
        self.assertFalse(client.kwargs["trust_env"])
        self.assertTrue(client.kwargs["verify"])
        self.assertTrue(client.closed)
        self.assertEqual([r[0] for r in client.requests], [IP_CHECK_URL, GEO_CHECK_URL + "198.51.100.7"])
        self.assertTrue(all(r[1]["allow_redirects"] is False for r in client.requests))

    async def test_network_error_never_retries_direct_or_saves_exception_secrets(self):
        FakeSession.responses = [RuntimeError("curl error at " + PROXY_URL)]
        async with self.sessions() as db:
            result = await ProxyProbe(FakeSession).test(db, self.proxy.id)
        self.assertEqual(result["test_status"], "offline")
        self.assertIsNone(result["exit_ip"])
        self.assertNotIn("private-password", json.dumps(result))
        self.assertEqual(len(FakeSession.instances), 1)

    async def test_geography_failure_does_not_misclassify_working_proxy(self):
        FakeSession.responses = [Response({"ip": "198.51.100.7"}), Response({}, status=429)]
        async with self.sessions() as db:
            result = await ProxyProbe(FakeSession).test(db, self.proxy.id)
        self.assertEqual(result["test_status"], "online")
        self.assertIsNone(result["country"])
        self.assertIn("地理信息", result["test_message"])

    async def test_failed_retest_clears_old_exit_instead_of_showing_stale_ip(self):
        async with self.sessions() as db:
            proxy = await db.get(ProxyEndpoint, self.proxy.id)
            proxy.exit_ip, proxy.country, proxy.test_status = "198.51.100.7", "美国", "online"
            await db.commit()
            FakeSession.responses = [RuntimeError("timeout")]
            result = await ProxyProbe(FakeSession).test(db, self.proxy.id)
        self.assertIsNone(result["exit_ip"])
        self.assertIsNone(result["country"])


class ProxyApiTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key="only-for-tests")
        self.app.include_router(proxies.router)

        async def db_override():
            async with self.sessions() as db:
                yield db
        self.app.dependency_overrides[get_db] = db_override
        self.app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_page_renders_shell_and_iframe_content_with_management_controls(self):
        shell = await self.client.get("/admin/proxies")
        self.assertEqual(shell.status_code, 200)
        self.assertIn('id="adminWorkspaceFrame"', shell.text)
        response = await self.client.get("/admin/proxies", headers={"Sec-Fetch-Dest": "iframe"})
        self.assertEqual(response.status_code, 200)
        for control in ("proxyPage", "proxyEditorForm", "proxyDefaultForm", "proxyAccountsPanel"):
            self.assertIn(f'id="{control}"', response.text)
        self.assertNotIn("private-password", response.text)

    async def test_all_management_routes_require_admin(self):
        self.app.dependency_overrides.pop(require_admin)
        for method, path in [("GET", "/data"), ("GET", ""), ("POST", "/nodes"),
                             ("PUT", "/nodes/1"), ("DELETE", "/nodes/1"),
                             ("POST", "/nodes/1/test"), ("PUT", "/default"), ("PUT", "/accounts")]:
            with self.subTest(path=path, method=method):
                response = await self.client.request(method, "/admin/proxies" + path, json={})
                self.assertEqual(response.status_code, 401)

    async def test_inventory_and_creation_never_return_proxy_password(self):
        response = await self.client.post("/admin/proxies/nodes", json={"name": "出口 2", "url": PROXY_URL})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("private-password", response.text)
        proxy_id = response.json()["proxy"]["id"]
        async with self.sessions() as db:
            proxy = await db.get(ProxyEndpoint, proxy_id)
            self.assertNotIn(PROXY_URL, proxy.url_encrypted)
            self.assertEqual(encryption_service.decrypt_token(proxy.url_encrypted), PROXY_URL)
        response = await self.client.get("/admin/proxies/data")
        self.assertNotIn("private-password", response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(len(response.json()["accounts"]), 3)

    async def test_edit_with_empty_address_preserves_credentials_and_changed_address_resets_test(self):
        async with self.sessions() as db:
            proxy = await db.get(ProxyEndpoint, self.proxy.id)
            proxy.exit_ip, proxy.test_status = "198.51.100.7", "online"
            await db.commit()
        response = await self.client.put(f"/admin/proxies/nodes/{self.proxy.id}", json={"name": "重命名", "url": ""})
        self.assertEqual(response.json()["proxy"]["test_status"], "online")
        async with self.sessions() as db:
            proxy = await db.get(ProxyEndpoint, self.proxy.id)
            self.assertEqual(encryption_service.decrypt_token(proxy.url_encrypted), PROXY_URL)
        response = await self.client.put(f"/admin/proxies/nodes/{self.proxy.id}", json={"name": "重命名", "url": "http://198.51.100.9:80"})
        self.assertEqual(response.json()["proxy"]["test_status"], "untested")
        self.assertIsNone(response.json()["proxy"]["exit_ip"])

    async def test_bound_proxy_cannot_be_deleted_and_can_be_removed_after_unbind(self):
        await self.client.put("/admin/proxies/accounts", json={"email": self.account.email, "mode": "proxy", "proxy_id": self.proxy.id})
        response = await self.client.delete(f"/admin/proxies/nodes/{self.proxy.id}")
        self.assertEqual(response.status_code, 409)
        await self.client.put("/admin/proxies/accounts", json={"email": self.account.email, "mode": "inherit"})
        self.assertEqual((await self.client.delete(f"/admin/proxies/nodes/{self.proxy.id}")).status_code, 200)

    async def test_default_route_also_prevents_deletion(self):
        response = await self.client.put("/admin/proxies/default", json={"mode": "proxy", "proxy_id": self.proxy.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client.delete(f"/admin/proxies/nodes/{self.proxy.id}")).status_code, 409)

    async def test_invalid_proxy_and_route_are_rejected(self):
        for url in ["ftp://host:21", "http://host:80/path", "http://host:80?secret=bad", "http://host", "http://host:abc"]:
            with self.subTest(url=url):
                response = await self.client.post("/admin/proxies/nodes", json={"name": "无效代理", "url": url})
                self.assertEqual(response.status_code, 400)
        response = await self.client.put("/admin/proxies/accounts", json={"email": self.account.email, "mode": "direct", "proxy_id": self.proxy.id})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
