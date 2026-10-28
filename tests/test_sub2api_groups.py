"""Live group reconciliation persists cleanup before account creation."""
import json
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Setting
from app.services.encryption import encryption_service
from app.services.settings import SettingsService
from app.services.sub2api import Sub2apiError, Sub2apiService


class Sub2apiGroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.db.add_all([Setting(key=key, value=value) for key, value in {
            "sub2api_base_url": "https://solidapi.top",
            "sub2api_api_key_encrypted": encryption_service.encrypt_token("test-key"),
            "sub2api_group_mode": "selected", "sub2api_group_ids": "[1, 2, 3]",
        }.items()])
        await self.db.commit()
        self.groups = [{"id": 1, "platform": "openai", "status": "active"},
                       {"id": 3, "platform": "openai", "status": "disabled"}]
        self.created = []
        self.service = Sub2apiService(lambda **options: httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), **options))
        self.settings = SettingsService()
        self.patch = patch("app.services.sub2api.settings_service", self.settings)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.db.close()
        await self.engine.dispose()

    def handle(self, request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": self.groups})
        self.created.append(json.loads(request.read()))
        return httpx.Response(200, json={"code": 0, "data": {"id": 42}})

    async def selected_ids(self):
        self.db.expire_all()
        row = (await self.db.execute(select(Setting).where(
            Setting.key == "sub2api_group_ids"
        ))).scalar_one()
        return json.loads(row.value)

    async def test_missing_and_disabled_groups_are_removed_and_valid_groups_import(self):
        self.settings._cache["sub2api_group_ids"] = "[99]"
        result = await self.service.import_member({"accounts": [{"name": "owner"}]}, self.db)
        self.assertEqual(result["group_count"], 1)
        self.assertEqual(self.created[0]["group_ids"], [1])
        self.assertEqual(await self.selected_ids(), [1])
        self.assertEqual(json.loads(self.settings._cache["sub2api_group_ids"]), [1])
        await self.service.import_member({"accounts": [{"name": "another"}]}, self.db)
        self.assertEqual(self.created[1]["group_ids"], [1])

    async def test_all_stale_groups_are_deleted_without_selecting_other_groups(self):
        self.groups = [{"id": 4, "platform": "openai", "status": "active"}]
        with self.assertRaisesRegex(Sub2apiError, "均已失效并自动移除"):
            await self.service.import_member({"accounts": [{"name": "owner"}]}, self.db)
        self.assertEqual(await self.selected_ids(), [])
        self.assertEqual(self.created, [])

    async def test_empty_remote_list_also_removes_stale_config(self):
        self.groups = []
        with self.assertRaisesRegex(Sub2apiError, "均已失效"):
            await self.service.import_member({"accounts": [{"name": "owner"}]}, self.db)
        self.assertEqual(await self.selected_ids(), [])
        self.assertEqual(self.created, [])

    async def test_failed_group_fetch_never_clears_saved_selection(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        service = Sub2apiService(lambda **options: httpx.AsyncClient(transport=transport, **options))
        with self.assertRaisesRegex(Sub2apiError, "HTTP 401"):
            await service.import_member({"accounts": [{"name": "owner"}]}, self.db)
        self.assertEqual(await self.selected_ids(), [1, 2, 3])

    async def test_failed_cleanup_does_not_create_account(self):
        with patch.object(self.settings, "update_setting", return_value=False):
            with self.assertRaisesRegex(Sub2apiError, "清理失效.*失败"):
                await self.service.import_member({"accounts": [{"name": "owner"}]}, self.db)
        self.assertEqual(self.created, [])
