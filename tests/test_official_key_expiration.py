"""官key 到期时间匹配、手动设置和共享展示组件的集成验证。"""
from datetime import datetime
import unittest

import httpx
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, get_db
from app.dependencies.auth import require_admin
from app.models import Team
from app.official_key_model import OfficialKeyExpiration, OfficialKeyExport
from app.routes import official_keys
from app.routes.official_keys import router


class BalanceStub:
    async def query_many(self, api_keys):
        return {entry_id: {"status": "error", "error": "测试余额未查询"}
                for entry_id in api_keys}


class OfficialKeyExpirationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        app = FastAPI()
        app.include_router(router)
        app.add_middleware(SessionMiddleware, secret_key="expiration-tests-only")

        async def session():
            async with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = session
        app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        app.dependency_overrides[official_keys.get_balance_service] = lambda: BalanceStub()
        self.app = app
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        response = await self.client.post("/admin/official-keys", json={
            "account": "Owner@Example.com", "api_key": "test-official-key",
        })
        self.entry = response.json()["key"]
        self.path = f"/admin/official-keys/{self.entry['id']}/expiration"

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def team(self, *, expires_at, email="owner@example.com"):
        async with self.sessions() as db:
            entry = Team(email=email, access_token_encrypted="test-only", expires_at=expires_at)
            db.add(entry)
            await db.commit()
            return entry.id

    async def view(self):
        response = await self.client.get("/admin/official-keys/data")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["keys"], [self.entry])
        return response.json()["expirations"][str(self.entry["id"])]

    async def test_case_insensitive_account_match_reads_live_expiry_and_shared_component(self):
        team_id = await self.team(expires_at=datetime(2026, 11, 30, 12, 45), email=" owner@EXAMPLE.com ")
        await self.team(expires_at=datetime(2027, 1, 1), email="unrelated@example.com")
        view = await self.view()
        self.assertEqual(view["source"], "team")
        self.assertFalse(view["can_edit"])
        self.assertEqual(view["expires_at"], "2026-11-30T12:45:00+08:00")
        from app.main import templates
        component = templates.env.get_template("components/team_expiration.html").module.team_expiration
        self.assertEqual(view["html"], str(component(datetime(2026, 11, 30, 12, 45))))
        async with self.sessions() as db:
            await db.execute(update(Team).where(Team.id == team_id).values(expires_at=datetime(2026, 12, 1)))
            await db.commit()
        self.assertEqual((await self.view())["expires_at"], "2026-12-01T00:00:00+08:00")

    async def test_missing_time_can_be_manually_set_updated_and_cleared(self):
        await self.team(expires_at=None)
        self.assertIsNone((await self.view())["expires_at"])
        for value, expected in (
            ("2026-12-10T15:30", "2026-12-10T15:30:00+08:00"),
            ("2026-12-11T07:00:00Z", "2026-12-11T15:00:00+08:00"),
        ):
            response = await self.client.put(self.path, json={"expires_at": value})
            self.assertEqual(response.status_code, 200, response.text)
            view = await self.view()
            self.assertEqual(view["expires_at"], expected)
            self.assertEqual(view["source"], "manual")
            self.assertTrue(view["can_edit"])
        response = await self.client.put(self.path, json={"expires_at": None})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone((await self.view())["expires_at"])
        async with self.sessions() as db:
            self.assertIsNone(await db.get(OfficialKeyExpiration, self.entry["id"]))

    async def test_workbench_takes_precedence_when_it_acquires_a_time(self):
        await self.client.put(self.path, json={"expires_at": "2026-12-10T15:30"})
        await self.team(expires_at=datetime(2026, 11, 1))
        view = await self.view()
        self.assertEqual(view["source"], "team")
        self.assertEqual(view["expires_at"], "2026-11-01T00:00:00+08:00")
        response = await self.client.put(self.path, json={"expires_at": "2027-01-01T00:00"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await self.view())["expires_at"], view["expires_at"])

    async def test_duplicate_workspaces_with_same_time_are_unambiguous(self):
        await self.team(expires_at=datetime(2026, 11, 1))
        await self.team(expires_at=datetime(2026, 11, 1))
        self.assertEqual((await self.view())["source"], "team")

    async def test_conflicting_workspaces_are_exposed_and_can_be_set_manually(self):
        await self.team(expires_at=datetime(2026, 11, 1))
        await self.team(expires_at=datetime(2026, 11, 2))
        view = await self.view()
        self.assertIsNone(view["expires_at"])
        self.assertIn("多个不同", view["note"])
        response = await self.client.put(self.path, json={"expires_at": "2026-11-03T00:00"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.view())["source"], "manual")

    async def test_invalid_input_missing_record_and_missing_admin_are_errors(self):
        for payload in ({"expires_at": "bad-time"}, {}):
            response = await self.client.put(self.path, json=payload)
            self.assertEqual(response.status_code, 422)
        response = await self.client.put("/admin/official-keys/999/expiration", json={"expires_at": None})
        self.assertEqual(response.status_code, 404)
        self.app.dependency_overrides.pop(require_admin)
        response = await self.client.put(self.path, json={"expires_at": None})
        self.assertEqual(response.status_code, 401)

    async def test_setting_expiry_preserves_key_and_export_and_delete_cleans_it_up(self):
        async with self.sessions() as db:
            db.add(OfficialKeyExport(official_key_id=self.entry["id"], base_url="https://example.test",
                                    operation_id="test", status="exported", account_id=42))
            await db.commit()
        response = await self.client.put(self.path, json={"expires_at": "2026-11-03T00:00"})
        self.assertEqual(response.status_code, 200)
        async with self.sessions() as db:
            self.assertEqual((await db.get(OfficialKeyExport, self.entry["id"])).status, "exported")
        response = await self.client.delete(f"/admin/official-keys/{self.entry['id']}")
        self.assertEqual(response.status_code, 200)
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(OfficialKeyExpiration))).scalars().all(), [])


if __name__ == "__main__":
    unittest.main()
