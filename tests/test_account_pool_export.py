import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, AccountPoolExportJob, AccountPoolWorkspace
from app.routes.admin import AccountPoolWorkspaceSelection, select_account_pool_workspace
from app.services.account_pool_authorization import AccountPoolLoginResult
from app.services.account_pool_export import AccountPoolExportService, selected_workspace


class AccountPoolExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.authorization = SimpleNamespace(
            login_entry=AsyncMock(return_value=AccountPoolLoginResult(
                payload={"accounts": [{"credentials": {"email": "member@example.com"}}]},
                workspace={"workspace_id": "team-b", "workspace_name": "Team B"},
            )),
            save_result=AsyncMock(return_value=True),
        )
        self.sub2api = SimpleNamespace(import_member=AsyncMock(
            return_value={"account_id": 42}
        ))
        self.records = SimpleNamespace(record_success=AsyncMock())
        self.service = AccountPoolExportService(
            self.sessions, self.authorization, self.sub2api, self.records
        )
        async with self.sessions() as session:
            session.add(AccountPoolEntry(
                id=1, email="member@example.com", workspace_id="team-a",
                workspace_state_json=json.dumps({"available_workspaces": [
                    {"id": "team-a"}, {"id": "team-b"},
                ]}),
            ))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_export_uses_selected_workspace_and_records_completion(self):
        async with self.sessions() as session:
            job = await self.service.enqueue(session, 1, "team-b")
            self.assertEqual(job.status, "pending")
        await self.service.run(job.id)
        async with self.sessions() as session:
            stored = await session.get(AccountPoolExportJob, job.id)
            self.assertEqual(stored.status, "completed")
            self.assertEqual(stored.sub2api_account_id, 42)
        self.authorization.login_entry.assert_awaited_once_with(
            unittest.mock.ANY, 1, "team-b"
        )
        self.assertFalse(self.authorization.save_result.await_args.kwargs["update_current"])
        self.records.record_success.assert_awaited_once()

    async def test_failed_import_is_visible_on_job(self):
        self.sub2api.import_member.side_effect = RuntimeError("remote unavailable")
        async with self.sessions() as session:
            job = await self.service.enqueue(session, 1, "team-b")
        with self.assertLogs("app.services.account_pool_export", level="ERROR"):
            await self.service.run(job.id)
        async with self.sessions() as session:
            stored = await session.get(AccountPoolExportJob, job.id)
            self.assertEqual(stored.status, "failed")
            self.assertIn("remote unavailable", stored.error)

    async def test_selection_switches_current_workspace_json(self):
        async with self.sessions() as session:
            session.add(AccountPoolWorkspace(
                account_pool_id=1, workspace_id="team-b", name="Team B",
                status="workspace_ok", export_json_encrypted="encrypted-team-b",
            ))
            await session.commit()
            result = await select_account_pool_workspace(
                1, AccountPoolWorkspaceSelection(workspace_id="team-b"), session, {}
            )
            entry = await session.get(AccountPoolEntry, 1)
        self.assertTrue(result["success"])
        self.assertEqual(entry.workspace_id, "team-b")
        self.assertEqual(entry.export_json_encrypted, "encrypted-team-b")

    def test_rejects_workspace_outside_scan(self):
        entry = AccountPoolEntry(workspace_id="team-a", workspace_state_json=json.dumps({
            "available_workspaces": [{"id": "team-a"}]
        }))
        with self.assertRaisesRegex(ValueError, "不在"):
            selected_workspace(entry, "team-b")
