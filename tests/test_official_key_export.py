"""Exercise real SQLite records and API routes against Sub2API HTTP contracts."""
import json
import secrets
import unittest
from copy import deepcopy
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, get_db
from app.dependencies.auth import require_admin
from app.models import Setting
from app.official_key_model import OfficialKeyExport
from app.routes import official_keys
from app.services.encryption import encryption_service
from app.services.official_key_export import OfficialKeyExportService
from app.services.official_key_sub2api import OfficialKeySub2api
from app.services.settings import SettingsService
from app.services.sub2api import Sub2apiService


class BalanceStub:
    async def query_many(self, api_keys):
        return {entry_id: {"status": "error", "error": "测试余额未查询"}
                for entry_id in api_keys}


class OfficialKeyExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        await self.prepare_database()
        self.prepare_template()
        self.groups = [{"id": 56, "name": "OpenAI 官 key", "platform": "openai", "status": "active"},
                       {"id": 99, "name": "oauth", "platform": "openai"}]
        self.pages = [[self.template]]
        self.writes = []
        self.failure = None
        self.target = {"id": 1001, "platform": "openai", "type": "apikey"}
        sub2api = Sub2apiService(lambda **options: httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), **options))
        self.exporter = OfficialKeyExportService(
            gateway=OfficialKeySub2api(sub2api), cipher=encryption_service)
        self.settings_patch = patch("app.services.sub2api.settings_service", SettingsService())
        self.settings_patch.start()
        await self.prepare_client()

    async def prepare_database(self):
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add_all([Setting(key=key, value=value) for key, value in {
                "sub2api_base_url": "https://sub.test",
                "sub2api_api_key_encrypted": encryption_service.encrypt_token("admin-key"),
                "sub2api_group_mode": "selected", "sub2api_group_ids": "[99]",
                "sub2api_default_concurrency": "3", "sub2api_excel_bps_enabled": "true",
            }.items()])
            await db.commit()

    def prepare_template(self):
        self.template = {
            "id": 933, "name": "官key", "platform": "openai", "type": "apikey",
            "credentials": {"api_key": "template-secret", "base_url": "https://api.openai.com",
                            "model_mapping": {"model-a": "model-b"}},
            "concurrency": 10, "priority": 1, "rate_multiplier": 0.7, "proxy_id": 5,
            "load_factor": 2, "auto_pause_on_expired": True,
            "expires_at": "2030-01-01T00:00:00Z",
            "extra": {"openai_apikey_responses_websockets_v2_mode": "off",
                      "upstream_billing_probe_enabled": True,
                      "upstream_billing_rate_sync_enabled": False,
                      "upstream_billing_probe": {"status": "unsupported"},
                      "upstream_model_metadata": {"models": {}}},
        }

    async def prepare_client(self):
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex())
        self.app.include_router(official_keys.router)
        self.app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        self.app.dependency_overrides[official_keys.get_export_service] = lambda: self.exporter
        self.app.dependency_overrides[official_keys.get_balance_service] = lambda: BalanceStub()

        async def session():
            async with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_db] = session
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        response = await self.client.post("/admin/official-keys", json={
            "account": "owner@example.com", "api_key": "actual-key"})
        self.entry = response.json()["key"]
        self.path = f"/admin/official-keys/{self.entry['id']}/sub2api"

    async def asyncTearDown(self):
        self.settings_patch.stop()
        await self.client.aclose()
        await self.engine.dispose()

    def handle(self, request):
        self.assertEqual(request.headers["x-api-key"], "admin-key")
        path = request.url.path
        if request.method != "GET":
            self.writes.append((request.method, path, json.loads(request.read())))
            self.assertTrue(request.headers["idempotency-key"])
            if self.failure == "timeout":
                raise httpx.ReadTimeout("read timeout", request=request)
            if self.failure == "connect":
                raise httpx.ConnectError("offline", request=request)
            if self.failure == "missing-id":
                return httpx.Response(200, json={"code": 0, "data": {}})
            if self.failure == "reject":
                return httpx.Response(400, json={"code": 400, "message": "bad request"})
            return httpx.Response(200, json={"code": 0, "data": self.target})
        if path.endswith("/groups/all"):
            data = self.groups
        elif path.endswith("/accounts/933"):
            data = self.template
        elif path.endswith("/accounts/1001"):
            data = self.target
        else:
            page = int(request.url.params["page"])
            data = {"items": self.pages[page - 1], "pages": len(self.pages)}
        return httpx.Response(200, json={"code": 0, "data": data})

    async def record(self):
        async with self.sessions() as db:
            return await db.get(OfficialKeyExport, self.entry["id"])

    async def test_export_clones_template_and_only_binds_official_group(self):
        original = deepcopy(self.template)
        response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["export"]["account_id"], 1001)
        method, path, sent = self.writes[0]
        self.assertEqual((method, path), ("POST", "/api/v1/admin/accounts"))
        self.assertEqual(sent["name"], "官key owner@example.com")
        self.assertEqual(sent["type"], "apikey")
        self.assertEqual(sent["group_ids"], [56])
        self.assertEqual(sent["credentials"], {
            **self.template["credentials"], "api_key": "actual-key"})
        for key in ("concurrency", "priority", "proxy_id", "rate_multiplier", "load_factor"):
            self.assertEqual(sent[key], self.template[key])
        self.assertEqual(sent["expires_at"], 1893456000)
        self.assertTrue(sent["upstream_billing_probe_enabled"])
        self.assertNotIn("upstream_billing_probe", sent["extra"])
        self.assertNotIn("upstream_model_metadata", sent["extra"])
        self.assertNotIn("openai_excel_bps", sent["extra"])
        self.assertEqual(self.template, original)
        self.assertEqual((await self.record()).status, "exported")
        listing = (await self.client.get("/admin/official-keys/data")).json()
        self.assertEqual(listing["exports"][str(self.entry["id"])]["account_id"], 1001)
        self.assertNotIn("actual-key", json.dumps(listing["exports"]))

    async def test_repeat_export_and_changed_key_update_same_account(self):
        await self.client.post(self.path)
        await self.client.post(self.path)
        self.assertEqual([write[0] for write in self.writes], ["POST", "PUT"])
        await self.client.put(f"/admin/official-keys/{self.entry['id']}", json={
            "account": "renamed@example.com", "api_key": "replacement-key"})
        self.assertEqual((await self.record()).status, "changed")
        response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 200)
        method, path, sent = self.writes[-1]
        self.assertEqual((method, path), ("PUT", "/api/v1/admin/accounts/1001"))
        self.assertEqual(sent["name"], "官key renamed@example.com")
        self.assertEqual(sent["credentials"]["api_key"], "replacement-key")

    async def test_template_search_reads_all_pages_and_requires_exact_unique_name(self):
        self.pages = [[{**self.template, "name": "官key someone"}], [self.template]]
        self.assertEqual((await self.client.post(self.path)).status_code, 200)
        self.pages[0].append(self.template)
        response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 400)
        self.assertIn("匹配 2 个", response.json()["detail"])
        self.assertEqual(len(self.writes), 1)

    async def test_missing_disabled_or_ambiguous_group_never_creates(self):
        official = self.groups[0]
        for groups in ([], [{**official, "status": "disabled"}], [official, {**official, "id": 57}]):
            self.groups = groups
            response = await self.client.post(self.path)
            self.assertEqual(response.status_code, 400)
            self.assertIn('分组「OpenAI 官 key」', response.json()["detail"])
        self.assertEqual(self.writes, [])

    async def test_previous_incorrect_group_name_does_not_select_an_unintended_group(self):
        self.groups[0]["name"] = "openai 官key"
        response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 400)
        self.assertIn('分组「OpenAI 官 key」', response.json()["detail"])
        self.assertIn("匹配 0 个", response.json()["detail"])
        self.assertEqual(self.writes, [])

    async def test_missing_template_error_identifies_template_instead_of_group(self):
        self.pages = [[]]
        response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 400)
        self.assertIn('模板账号「官key」', response.json()["detail"])
        self.assertEqual(self.writes, [])

    async def test_wrong_template_type_is_not_exported_as_oauth(self):
        self.template["type"] = "oauth"
        response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 400)
        self.assertIn("API Key", response.json()["detail"])
        self.assertEqual(self.writes, [])

    async def test_uncertain_creation_is_visible_and_never_automatically_recreated(self):
        for failure in ("timeout", "missing-id"):
            self.failure = failure
            response = await self.client.post(self.path)
            self.assertEqual(response.status_code, 409)
            self.assertEqual((await self.record()).status, "uncertain")
            response = await self.client.post(self.path)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(len(self.writes), 1)
            async with self.sessions() as db:
                record = await db.get(OfficialKeyExport, self.entry["id"])
                await db.delete(record)
                await db.commit()
            self.writes.clear()

    async def test_confirmed_rejection_or_connect_failure_can_be_retried(self):
        for failure in ("reject", "connect"):
            self.failure = failure
            self.assertEqual((await self.client.post(self.path)).status_code, 400)
            self.assertEqual((await self.record()).status, "failed")
        self.failure = None
        self.assertEqual((await self.client.post(self.path)).status_code, 200)

    async def test_target_type_changes_or_site_changes_do_not_overwrite_accounts(self):
        await self.client.post(self.path)
        self.target["type"] = "oauth"
        self.assertEqual((await self.client.post(self.path)).status_code, 400)
        self.target["type"] = "apikey"
        async with self.sessions() as db:
            record = await db.get(OfficialKeyExport, self.entry["id"])
            record.base_url = "https://old-site.test"
            await db.commit()
        self.assertEqual((await self.client.post(self.path)).status_code, 400)
        self.assertEqual(len(self.writes), 1)

    async def test_running_export_cannot_be_duplicated_or_edited(self):
        async with self.sessions() as db:
            db.add(OfficialKeyExport(official_key_id=self.entry["id"], base_url="https://sub.test",
                                     status="exporting", operation_id="running"))
            await db.commit()
        self.assertEqual((await self.client.post(self.path)).status_code, 400)
        changed = await self.client.put(f"/admin/official-keys/{self.entry['id']}", json={
            "account": "new", "api_key": "new-key"})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.writes, [])

    async def test_export_route_requires_admin_and_reports_missing_keys(self):
        self.assertEqual((await self.client.post("/admin/official-keys/999/sub2api")).status_code, 404)
        del self.app.dependency_overrides[require_admin]
        self.assertEqual((await self.client.post(self.path)).status_code, 401)


if __name__ == "__main__":
    unittest.main()
