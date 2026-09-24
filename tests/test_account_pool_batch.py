import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, AccountPoolTotpJob, AccountPoolWorkspace
from app.services.account_pool_batch import AccountPoolBatchService
from app.services.account_pool_totp import AccountPoolTotpError


class Cipher:
    def encrypt_token(self, value):
        return f"encrypted:{value}"

    def decrypt_token(self, value):
        return value.removeprefix("encrypted:")


def snapshot(email):
    return json.dumps({"type": "sub2api-data", "version": 1,
                       "accounts": [{"name": email}]})


class AccountPoolBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession,
                                          expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.cipher = Cipher()
        self.totp = SimpleNamespace(rotate=AsyncMock(return_value="NEWSECRET"))
        self.service = AccountPoolBatchService(self.sessions, self.totp, self.cipher)
        async with self.sessions() as session:
            session.add_all([
                AccountPoolEntry(id=1, email="one@example.com"),
                AccountPoolEntry(id=2, email="two@example.com"),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_merged_json_includes_every_saved_workspace(self):
        async with self.sessions() as session:
            session.add_all([
                AccountPoolWorkspace(account_pool_id=1, workspace_id="a", status="ok",
                    export_json_encrypted=self.cipher.encrypt_token(snapshot("one-a"))),
                AccountPoolWorkspace(account_pool_id=1, workspace_id="b", status="ok",
                    export_json_encrypted=self.cipher.encrypt_token(snapshot("one-b"))),
                AccountPoolWorkspace(account_pool_id=2, workspace_id="c", status="ok",
                    export_json_encrypted=self.cipher.encrypt_token(snapshot("two-c"))),
            ])
            await session.commit()
            result = await self.service.merged_json(session, [1, 2])
        self.assertEqual(result["type"], "sub2api-data")
        self.assertEqual({item["name"] for item in result["accounts"]},
                         {"one-a", "one-b", "two-c"})

    async def test_missing_snapshot_fails_whole_export(self):
        async with self.sessions() as session:
            with self.assertRaisesRegex(ValueError, "没有已保存的 JSON"):
                await self.service.merged_json(session, [1, 2])

    async def test_sub2api_jobs_cover_each_saved_workspace(self):
        async with self.sessions() as session:
            entry = await session.get(AccountPoolEntry, 1)
            entry.workspace_state_json = json.dumps({"available_workspaces": [
                {"id": "a"}, {"id": "b"},
            ]})
            session.add_all([
                AccountPoolWorkspace(account_pool_id=1, workspace_id="a", status="ok",
                    export_json_encrypted=self.cipher.encrypt_token(snapshot("one-a"))),
                AccountPoolWorkspace(account_pool_id=1, workspace_id="b", status="ok",
                    export_json_encrypted=self.cipher.encrypt_token(snapshot("one-b"))),
            ])
            await session.commit()
            jobs = await self.service.enqueue_exports(session, [1])
        self.assertEqual({job.workspace_id for job in jobs}, {"a", "b"})

    async def test_rotation_audits_success_and_failure_without_losing_other_jobs(self):
        self.totp.rotate.side_effect = ["NEWSECRET", AccountPoolTotpError("远端失败")]
        async with self.sessions() as session:
            batch_id = await self.service.enqueue_rotations(session, [1, 2])
        with self.assertLogs("app.services.account_pool_batch", level="ERROR"):
            await self.service.run_rotations(batch_id)
        async with self.sessions() as session:
            results = await self.service.rotation_status(session, batch_id)
            total, history = await self.service.rotation_history(session, 1, 20)
        self.assertEqual([item["status"] for item in results], ["completed", "failed"])
        self.assertEqual(results[0]["two_factor_secret"], "NEWSECRET")
        self.assertIn("远端失败", results[1]["error"])
        self.assertEqual(total, 2)
        self.assertEqual([item["email"] for item in history],
                         ["two@example.com", "one@example.com"])

    async def test_delete_rejects_active_rotation_and_keeps_audit(self):
        async with self.sessions() as session:
            batch_id = await self.service.enqueue_rotations(session, [1])
            with self.assertRaisesRegex(ValueError, "后台任务"):
                await self.service.delete(session, [1])
            await session.rollback()
        await self.service.run_rotations(batch_id)
        async with self.sessions() as session:
            self.assertEqual(await self.service.delete(session, [1]), ["one@example.com"])
            self.assertIsNotNone((await session.execute(
                select(AccountPoolTotpJob).where(
                    AccountPoolTotpJob.batch_id == batch_id)
            )).scalar_one_or_none())
