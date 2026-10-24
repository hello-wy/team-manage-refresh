"""Quota retrieval must run after cached page rendering and survive page reloads."""
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, get_db
from app.dependencies.auth import require_admin
from app.models import AccountPoolEntry, QuotaSnapshot, Team, TeamEmailMapping
from app.routes import admin
from app.services.encryption import encryption_service
from app.utils.time_utils import get_now

EMAIL = "member@example.com"
SPACE_ID = "team-a"
USAGE = {"status": "ok", "error": None, "1week": {
    "used": 25, "remaining": 75, "limit": 100, "reset_at": "1792649621",
}}


class AccountPoolQuotaRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.sessions()
        self.entry = AccountPoolEntry(email=EMAIL, workspace_id="personal", workspace_status="personal_account")
        self.team = Team(email="owner@example.com", account_id=SPACE_ID, access_token_encrypted="unused")
        self.db.add_all([self.entry, self.team])
        await self.db.flush()
        self.db.add(TeamEmailMapping(team_id=self.team.id, email=EMAIL, status="joined", seat_type="premium"))
        await self.db.commit()
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key="test-session-key")
        self.app.include_router(admin.router)
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[require_admin] = lambda: {"username": "admin", "is_admin": True}

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def page(self):
        request = Request({"type": "http", "method": "GET", "path": "/admin/account-pool",
                           "headers": [], "query_string": b"", "server": ("test", 80)})
        page = await admin.account_pool_page(request, search=EMAIL, db=self.db, current_user={"username": "admin"})
        return page.body.decode()

    async def post(self, path, payload=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            return await client.post(path, json=payload)

    async def snapshot(self):
        return (await self.db.execute(select(QuotaSnapshot).where(QuotaSnapshot.email == EMAIL))).scalar_one()

    async def test_initial_page_keeps_refresh_for_missing_quota_without_blocking_on_network(self):
        with patch.object(admin.account_pool_usage_service, "check_many", new=AsyncMock()) as network:
            html = await self.page()
        network.assert_not_awaited()
        self.assertIn('class="btn btn-sm btn-icon btn-minimal btn-secondary account-pool-quota-refresh"', html)
        self.assertIn('data-workspace-id="team-a"', html)
        self.assertIn('/static/js/account-pool-quota.js?', html)
        self.assertIn("未获取", html)

    async def test_visible_batch_uses_joined_team_and_persists_quota_for_reload(self):
        self.entry.export_json_encrypted = encryption_service.encrypt_token(json.dumps({
            "accounts": [{"credentials": {"chatgpt_account_id": "personal", "access_token": "personal-token"}}],
        }))
        await self.db.commit()
        network = AsyncMock(return_value={(EMAIL, SPACE_ID): USAGE})
        with patch.object(admin.account_pool_usage_service, "check_many", new=network):
            response = await self.post("/admin/account-pool/usage", {"entry_ids": [self.entry.id]})
        self.assertEqual(response.status_code, 200)
        item = response.json()["entries"][0]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["workspace_id"], SPACE_ID)
        self.assertIn("不适用", item["html"])
        self.assertIn('value="25"', item["html"])
        network.assert_awaited_once_with(self.db, [(EMAIL, SPACE_ID)])
        snapshot = await self.snapshot()
        self.assertEqual(snapshot.weekly_remaining, 75)
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(self.entry.workspace_id, "personal")
        self.assertIn('value="25"', await self.page())

    async def test_manual_refresh_replaces_invalid_snapshot_and_updates_saved_json(self):
        self.entry.workspace_id = SPACE_ID
        self.entry.export_json_encrypted = encryption_service.encrypt_token(json.dumps({"accounts": [{
            "credentials": {"chatgpt_account_id": SPACE_ID, "access_token": "team-token"},
        }]}))
        self.db.add(QuotaSnapshot(email=EMAIL, team_space_id=SPACE_ID, status="invalid",
                                  error="Token 无效", observed_at=get_now()))
        await self.db.commit()
        with patch.object(admin.account_pool_usage_service, "check_many", new=AsyncMock(return_value={(EMAIL, SPACE_ID): USAGE})):
            response = await self.post(f"/admin/account-pool/{self.entry.id}/usage")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual((await self.snapshot()).status, "ok")
        payload = json.loads(encryption_service.decrypt_token(self.entry.export_json_encrypted))
        self.assertEqual(payload["accounts"][0]["quota"]["1week"]["remaining"], 75)
        html = await self.page()
        quota_cell = html.partition('<td data-label="额度">')[2].split("</td>", 1)[0]
        self.assertNotIn("登录已失效", quota_cell)
        self.assertIn('value="25"', html)

    async def test_failed_quota_is_explicit_and_keeps_retry_available(self):
        invalid = {"status": "invalid", "error": "Token 无效"}
        with patch.object(admin.account_pool_usage_service, "check_many", new=AsyncMock(return_value={(EMAIL, SPACE_ID): invalid})):
            response = await self.post("/admin/account-pool/usage", {"entry_ids": [self.entry.id]})
        item = response.json()["entries"][0]
        self.assertEqual(item["status"], "invalid")
        self.assertEqual(item["error"], "Token 无效")
        self.assertNotIn("<progress", item["html"])
        self.assertEqual((await self.snapshot()).status, "invalid")
        html = await self.page()
        self.assertIn("account-pool-quota-refresh", html)
        self.assertIn("登录已失效", html)

    async def test_invalid_member_token_is_refreshed_and_retried_once(self):
        self.entry.workspace_id = SPACE_ID
        self.entry.password_encrypted = "saved-password"
        self.entry.two_factor_secret_encrypted = "saved-2fa"
        self.entry.export_json_encrypted = encryption_service.encrypt_token(json.dumps({"accounts": [{
            "credentials": {"chatgpt_account_id": SPACE_ID, "access_token": "old-token"},
        }]}))
        await self.db.commit()
        invalid = {"status": "invalid", "error": "Token 无效"}
        network = AsyncMock(side_effect=[
            {(EMAIL, SPACE_ID): invalid},
            {(EMAIL, SPACE_ID): USAGE},
        ])
        login = object()
        authorization = type("Authorization", (), {
            "login_entry": AsyncMock(return_value=login),
            "save_result": AsyncMock(return_value=True),
        })()
        with patch.object(admin.account_pool_usage_service, "check_many", new=network), \
             patch.object(admin, "account_pool_authorization_service", authorization):
            response = await self.post(f"/admin/account-pool/{self.entry.id}/usage")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(network.await_count, 2)
        authorization.login_entry.assert_awaited_once_with(self.db, self.entry.id, SPACE_ID)
        authorization.save_result.assert_awaited_once()
        self.assertEqual((await self.snapshot()).status, "ok")

    async def test_batch_rejects_missing_accounts_and_requires_admin(self):
        response = await self.post("/admin/account-pool/usage", {"entry_ids": [self.entry.id, 999]})
        self.assertEqual(response.status_code, 404)
        self.app.dependency_overrides.pop(require_admin)
        response = await self.post("/admin/account-pool/usage", {"entry_ids": [self.entry.id]})
        self.assertEqual(response.status_code, 401)
