"""真实 SQLite、登录会话、密码校验与官key 接口集成测试。"""
import secrets
import unittest
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, get_db
from app.models import Setting
from app.official_key_model import OfficialKey
from app.routes import auth, official_keys
from app.services.auth import auth_service
from app.services.encryption import encryption_service


class OfficialKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.password = secrets.token_urlsafe()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add(Setting(key="admin_password_hash", value=auth_service.hash_password(self.password)))
            await db.commit()
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex())
        self.app.include_router(auth.router)
        self.app.include_router(official_keys.router)

        class BalanceStub:
            async def query_many(self, api_keys):
                return {entry_id: {"status": "error", "error": "测试余额未查询"}
                        for entry_id in api_keys}

        self.balance_stub = BalanceStub()
        self.app.dependency_overrides[official_keys.get_balance_service] = lambda: self.balance_stub

        async def session():
            async with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_db] = session
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def login(self):
        response = await self.client.post("/auth/login", json={"password": self.password})
        self.assertEqual(response.status_code, 200, response.text)

    async def create(self, *, account="owner@example.com", api_key="test-key"):
        response = await self.client.post("/admin/official-keys", json={
            "account": account, "api_key": api_key,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["key"]

    async def test_management_requires_admin(self):
        for method, path in (
            ("GET", "/admin/official-keys"),
            ("GET", "/admin/official-keys/data"),
            ("POST", "/admin/official-keys"),
            ("PUT", "/admin/official-keys/1"),
            ("DELETE", "/admin/official-keys/1"),
            ("POST", "/admin/official-keys/1/sub2api"),
            ("GET", "/admin/official-keys/1/balance"),
        ):
            response = await self.client.request(method, path, json={"account": "a", "api_key": "b"})
            self.assertEqual(response.status_code, 401, (method, path, response.text))

    async def test_crud_encrypts_at_rest_and_exports_every_account(self):
        await self.login()
        first = await self.create(account="  first  ", api_key="  first-key  ")
        second = await self.create(account="first", api_key="second-key")
        async with self.sessions() as db:
            stored = await db.get(OfficialKey, first["id"])
            self.assertEqual(stored.account, "first")
            self.assertNotIn("first-key", stored.api_key_encrypted)
            self.assertEqual(encryption_service.decrypt_token(stored.api_key_encrypted), "first-key")
        response = await self.client.get("/api/official-keys", headers={"admintoken": self.password})
        self.assertEqual(response.json()["keys"], [second, first])
        self.assertEqual(response.headers["cache-control"], "no-store")
        changed = {"account": "updated", "api_key": "updated-key"}
        response = await self.client.put(f"/admin/official-keys/{first['id']}", json=changed)
        self.assertEqual(response.status_code, 200)
        response = await self.client.get("/admin/official-keys/data")
        self.assertEqual(response.json()["keys"][1], {"id": first["id"], **changed})
        response = await self.client.delete(f"/admin/official-keys/{second['id']}")
        self.assertEqual(response.status_code, 200)
        response = await self.client.get("/api/official-keys", headers={"admintoken": self.password})
        self.assertEqual(response.json()["keys"], [{"id": first["id"], **changed}])

    async def test_export_requires_header_even_with_a_logged_in_session(self):
        await self.login()
        await self.create()
        for path, headers in (
            ("/api/official-keys", {}),
            ("/api/official-keys", {"admintoken": "incorrect"}),
            ("/api/official-keys", {"X-API-Key": self.password}),
            ("/api/official-keys", {"Authorization": f"Bearer {self.password}"}),
            (f"/api/official-keys?admintoken={self.password}", {}),
        ):
            response = await self.client.get(path, headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("test-key", response.text)
        self.client.cookies.clear()
        response = await self.client.get("/api/official-keys", headers={"admintoken": self.password})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["keys"]), 1)

    async def test_password_change_immediately_revokes_old_token(self):
        await self.login()
        new_password = secrets.token_urlsafe()
        response = await self.client.post("/auth/change-password", json={
            "old_password": self.password, "new_password": new_password,
        })
        self.assertEqual(response.status_code, 200, response.text)
        old = await self.client.get("/api/official-keys", headers={"admintoken": self.password})
        new = await self.client.get("/api/official-keys", headers={"admintoken": new_password})
        self.assertEqual(old.status_code, 401)
        self.assertEqual(new.status_code, 200)
        self.assertEqual(new.json()["keys"], [])

    async def test_invalid_or_blank_fields_cannot_create_or_overwrite_keys(self):
        await self.login()
        entry = await self.create()
        for payload in (
            {"account": " ", "api_key": "key"}, {"account": "a", "api_key": "\n "},
            {"account": 42, "api_key": "key"}, {"account": "a"},
        ):
            for method, path in (
                ("POST", "/admin/official-keys"), ("PUT", f"/admin/official-keys/{entry['id']}"),
            ):
                response = await self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 422, response.text)
        response = await self.client.get("/admin/official-keys/data")
        self.assertEqual(response.json()["keys"], [entry])

    async def test_missing_rows_report_404(self):
        await self.login()
        edited = await self.client.put("/admin/official-keys/999", json={"account": "a", "api_key": "b"})
        deleted = await self.client.delete("/admin/official-keys/999")
        balance = await self.client.get("/admin/official-keys/999/balance")
        self.assertEqual(edited.status_code, 404)
        self.assertEqual(deleted.status_code, 404)
        self.assertEqual(balance.status_code, 404)

    async def test_balance_query_decrypts_key_on_server(self):
        await self.login()
        entry = await self.create(api_key="official-secret")

        class BalanceStub:
            def __init__(self):
                self.api_key = None

            async def query(self, api_key):
                self.api_key = api_key
                return {
                    "total_granted": "10", "total_used": "2.5",
                    "total_available": "7.5", "currency": "USD",
                }

        stub = BalanceStub()
        self.app.dependency_overrides[official_keys.get_balance_service] = lambda: stub
        response = await self.client.get(f"/admin/official-keys/{entry['id']}/balance")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(stub.api_key, "official-secret")
        self.assertEqual(response.json()["balance"]["total_available"], "7.5")
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_list_automatically_queries_each_stored_key(self):
        await self.login()
        first = await self.create(api_key="first-private-key")
        second = await self.create(api_key="second-private-key")
        balance = {"status": "ok", "balance": {
            "total_granted": "20", "total_used": "3.25", "total_available": "16.75",
            "currency": "USD",
        }}
        query = AsyncMock(return_value={
            first["id"]: balance,
            second["id"]: {"status": "error", "error": "HTTP 401: session required"},
        })
        self.balance_stub.query_many = query

        response = await self.client.get("/admin/official-keys/data")

        self.assertEqual(response.status_code, 200)
        query.assert_awaited_once_with({
            second["id"]: "second-private-key", first["id"]: "first-private-key",
        })
        self.assertEqual(response.json()["balances"][str(first["id"])], balance)
        self.assertIn("HTTP 401", response.json()["balances"][str(second["id"])]["error"])

    async def test_page_renders_navigation_and_does_not_embed_saved_keys(self):
        await self.login()
        await self.create(api_key="must-not-be-embedded-in-html")
        response = await self.client.get("/admin/official-keys")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('id="officialKeyForm"', response.text)
        self.assertIn('href="/admin/official-keys" class="desktop-nav-link active"', response.text)
        self.assertIn('id="officialKeyRows"', response.text)
        self.assertNotIn("must-not-be-embedded-in-html", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_uninitialized_password_is_an_explicit_error(self):
        async with self.sessions() as db:
            await db.execute(update(Setting).where(Setting.key == "admin_password_hash").values(value=""))
            await db.commit()
        response = await self.client.get("/api/official-keys", headers={"admintoken": self.password})
        self.assertEqual(response.status_code, 503)

    async def test_corrupt_ciphertext_surfaces_instead_of_silently_omitting_key(self):
        await self.login()
        await self.create()
        async with self.sessions() as db:
            await db.execute(update(OfficialKey).values(api_key_encrypted="corrupt"))
            await db.commit()
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/official-keys", headers={"admintoken": self.password})
        self.assertEqual(response.status_code, 500)


if __name__ == "__main__":
    unittest.main()
