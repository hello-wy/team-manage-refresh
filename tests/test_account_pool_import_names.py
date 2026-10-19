import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry
from app.services.account_pool_credentials import AccountPoolCredentialService
from app.services.account_pool_import import AccountPoolImportService
from app.services.account_pool_profile import save_import_account_name
from app.services.encryption import encryption_service

EMAIL = "import@example.com"
SECRET = "JBSWY3DPEHPK3PXP"
NAME = "Import Name"


def payload(email=EMAIL):
    claims = base64.urlsafe_b64encode(json.dumps({"email": email, "name": NAME}).encode()).decode()
    return {"id_token": f"header.{claims}.signature"}


class ImportNameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.sessions()
        self.credentials = AccountPoolCredentialService(encryption_service)
        self.authorization = SimpleNamespace(login_entry=AsyncMock(return_value=SimpleNamespace(payload=payload())))
        self.rotations = SimpleNamespace(enqueue_import_rotations=AsyncMock(), run_rotations=AsyncMock())
        self.service = AccountPoolImportService(self.authorization, self.rotations, save_import_account_name)

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def import_account(self, *, email=EMAIL, credentials=True):
        content = f"{email}----password----{SECRET}" if credentials else email
        result = await self.credentials.add_accounts(self.db, content=content)
        return await self.service.complete(self.db, result, background_tasks=BackgroundTasks(), rotate_2fa=False)

    async def test_new_import_saves_name_without_exporting_tokens_or_relogging_on_repeat(self):
        first = await self.import_account()
        entry = (await self.db.execute(select(AccountPoolEntry))).scalar_one()
        self.assertEqual((entry.name, entry.name_key), (NAME, "import name"))
        self.assertIsNone(entry.export_json_encrypted)
        self.assertEqual(first["profiles"]["saved"], [EMAIL])
        repeated = await self.import_account()
        self.assertEqual(repeated["updated"], [EMAIL])
        self.authorization.login_entry.assert_awaited_once()
        self.rotations.enqueue_import_rotations.assert_not_awaited()

    async def test_missing_credentials_are_reported_without_login(self):
        result = await self.import_account(credentials=False)
        self.assertEqual(result["profiles"]["skipped"], [
            {"email": EMAIL, "reason": "缺少登录密码或 2FA 密钥"}])
        self.authorization.login_entry.assert_not_awaited()

    async def test_failure_is_visible_and_repeat_import_does_not_retry(self):
        self.authorization.login_entry.side_effect = RuntimeError("upstream login rejected")
        with self.assertLogs("app.services.account_pool_import", level="ERROR"):
            result = await self.import_account()
        self.assertEqual(result["profiles"]["failed"], [
            {"email": EMAIL, "error": "upstream login rejected"}])
        self.assertTrue(result["success"])
        await self.import_account()
        self.authorization.login_entry.assert_awaited_once()

    async def test_import_2fa_queues_name_capture_without_a_second_login(self):
        result = await self.credentials.add_accounts(self.db, content=f"{EMAIL}----password----{SECRET}")
        self.rotations.enqueue_import_rotations.return_value = {
            "batch_id": "import-batch", "queued": 1, "skipped": []}
        tasks = BackgroundTasks()
        await self.service.complete(self.db, result, background_tasks=tasks, rotate_2fa=True)
        self.authorization.login_entry.assert_not_awaited()
        await tasks()
        self.rotations.run_rotations.assert_awaited_once_with("import-batch", populate_names=True)

    async def test_wrong_account_name_is_rejected(self):
        self.authorization.login_entry.return_value.payload = payload("other@example.com")
        with self.assertLogs("app.services.account_pool_import", level="ERROR"):
            result = await self.import_account()
        self.assertIn("邮箱与号池账号不一致", result["profiles"]["failed"][0]["error"])
        self.assertIsNone((await self.db.execute(select(AccountPoolEntry.name))).scalar_one())
