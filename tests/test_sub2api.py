import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services.encryption import encryption_service
from app.services.sub2api import Sub2apiError, Sub2apiService, apply_export_settings, normalize_base_url


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
        payload = {"accounts": [{"name": "member@example.com", "type": "oauth", "platform": "openai",
                                 "credentials": {"refresh_token": "secret"}}]}
        with patch("app.services.sub2api.settings_service.get_setting", new=AsyncMock(
                side_effect=["https://solidapi.top", encryption_service.encrypt_token("admin-secret"),
                             "all", "10", "off", "false"])):
            result = await service.import_member(payload, object())
        self.assertEqual(result, {"account_id": 42, "group_count": 2})
        self.assertEqual(requests[1].url.path, "/api/v1/admin/accounts")
        self.assertEqual(requests[1].headers["x-api-key"], "admin-secret")
        sent = json.loads(requests[1].read())
        self.assertEqual(sent["name"], "member@example.com")
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
        settings = ["https://solidapi.top", encryption_service.encrypt_token("key"), "selected", "[2]", "7", "session", "true"]
        with patch("app.services.sub2api.settings_service.get_setting", new=AsyncMock(side_effect=settings)):
            result = await service.import_member({"accounts": [{"name": "member"}]}, object())
        self.assertEqual(result["group_count"], 1)
        sent = json.loads(requests[1].read())
        self.assertEqual(sent["group_ids"], [2])
        self.assertEqual(sent["concurrency"], 7)
        self.assertEqual(sent["extra"]["codex_fingerprint_mode"], "session")
        self.assertIs(sent["extra"]["openai_excel_bps"], True)
        self.assertIs(sent["extra"]["openai_excel_bps_auto_disable_on_403"], True)
        self.assertIs(sent["extra"]["openai_excel_bps_cache_creation_as_input"], True)

    async def test_export_switch_overrides_saved_bps_without_mutating_credentials_or_input(self):
        payload = {"accounts": [{"type": "oauth", "credentials": {"access_token": "test-token"},
                                 "extra": {"openai_excel_bps": True,
                                           "openai_excel_bps_auto_disable_on_403": True,
                                           "openai_excel_bps_cache_creation_as_input": True,
                                           "other_option": "keep"}},
                                {"type": "oauth", "credentials": {"access_token": "second-token"},
                                 "extra": {"openai_excel_bps_auto_disable_on_403": False,
                                           "openai_excel_bps_cache_creation_as_input": False}}]}
        original = json.dumps(payload)
        for enabled in (False, True):
            with self.subTest(enabled=enabled), patch(
                "app.services.sub2api.settings_service.get_setting",
                new=AsyncMock(side_effect=["10", "off", "true" if enabled else "false"]),
            ):
                result = await apply_export_settings(payload, object())
                for account in result["accounts"]:
                    for key in ("openai_excel_bps", "openai_excel_bps_auto_disable_on_403",
                                "openai_excel_bps_cache_creation_as_input"):
                        if enabled:
                            self.assertIs(account["extra"][key], True)
                        else:
                            self.assertNotIn(key, account.get("extra", {}))
                self.assertEqual(result["accounts"][0]["extra"]["other_option"], "keep")
                self.assertEqual(result["accounts"][0]["credentials"], payload["accounts"][0]["credentials"])
                self.assertEqual(json.dumps(payload), original)

    async def test_export_reads_saved_setting_even_with_stale_worker_cache(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from app.database import Base
        from app.models import Setting
        from app.services.settings import SettingsService
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        service = SettingsService()
        service._cache["sub2api_excel_bps_enabled"] = "false"
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine)() as db:
                db.add(Setting(key="sub2api_excel_bps_enabled", value="true"))
                await db.commit()
                with patch("app.services.sub2api.settings_service", service):
                    result = await apply_export_settings({"accounts": [{"type": "oauth"}]}, db)
                extra = result["accounts"][0]["extra"]
                self.assertIs(extra["openai_excel_bps"], True)
                self.assertIs(extra["openai_excel_bps_auto_disable_on_403"], True)
                self.assertIs(extra["openai_excel_bps_cache_creation_as_input"], True)
        finally:
            await engine.dispose()

    async def test_no_groups_blocks_creation(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": []})

        service = Sub2apiService(lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs))
        with patch("app.services.sub2api.settings_service.get_setting", new=AsyncMock(
                side_effect=["https://solidapi.top", encryption_service.encrypt_token("key"), "all"])):
            with self.assertRaisesRegex(Sub2apiError, "没有可用"):
                await service.import_member({"accounts": [{"name": "member"}]}, object())
        self.assertEqual(len(requests), 1)

    def test_rejects_non_root_url(self):
        for value in ("file:///tmp/x", "https://host/path", "https://user:pass@host", "https://host?q=1"):
            with self.subTest(value=value), self.assertRaises(Sub2apiError):
                normalize_base_url(value)
