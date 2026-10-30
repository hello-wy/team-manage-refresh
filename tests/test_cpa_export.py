"""CPA jobs persist success/failure and never write Sub2API export records."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cpa_export_model import CpaExportJob
from app.database import Base, get_db
from app.dependencies.auth import require_admin
from app.models import AccountPoolEntry, Sub2apiExportRecord
from app.routes import cpa_export
from app.services.account_pool_authorization import AccountPoolLoginResult
from app.services.cpa import CpaExportError
from app.services.cpa_export import AccountPoolCpaExportService, attach_cpa_export_jobs


class CpaExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add(AccountPoolEntry(
                id=1, email="member@example.com", workspace_id="workspace-a",
                workspace_state_json=json.dumps({"available_workspaces": [
                    {"id": "workspace-a"}, {"id": "workspace-b"}]}),
            ))
            await session.commit()
        self.authorization = SimpleNamespace(
            login_entry=AsyncMock(return_value=AccountPoolLoginResult(
                payload={"verified": True}, workspace={"workspace_id": "workspace-b"})),
            save_result=AsyncMock(return_value=True),
        )
        self.cpa = SimpleNamespace(import_member=AsyncMock(return_value={"filename": "verified.json"}))
        self.service = AccountPoolCpaExportService(self.sessions, self.authorization, cpa=self.cpa)
        self.app = FastAPI()
        self.app.include_router(cpa_export.router)
        self.app.dependency_overrides[get_db] = self.get_db
        self.app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
        self.app.dependency_overrides[cpa_export.get_pool_export_service] = lambda: self.service
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def get_db(self):
        async with self.sessions() as session:
            yield session

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def test_authenticated_endpoint_queues_selected_workspace_and_exposes_completed_job(self):
        response = await self.client.post("/admin/account-pool/1/export-cpa", json={"workspace_id": "workspace-b"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        job_id = response.json()["job_id"]
        status = await self.client.get(f"/admin/account-pool/cpa-export-jobs/{job_id}")
        self.assertEqual(status.json()["status"], "completed")
        self.assertEqual(status.json()["filename"], "verified.json")
        self.authorization.login_entry.assert_awaited_once_with(unittest.mock.ANY, 1, "workspace-b")
        self.assertFalse(self.authorization.save_result.await_args.kwargs["update_current"])
        async with self.sessions() as session:
            rows = await attach_cpa_export_jobs(session, [{"id": 1}])
            self.assertEqual(rows[0]["cpa_export_job"].status, "completed")
            self.assertEqual(await session.scalar(select(func.count()).select_from(Sub2apiExportRecord)), 0)

    async def test_failed_upload_is_persisted_and_visible_without_success(self):
        self.cpa.import_member.side_effect = CpaExportError("CPA HTTP 401")
        with self.assertLogs("app.services.account_pool_export_job", level="ERROR"):
            response = await self.client.post("/admin/account-pool/1/export-cpa", json={"workspace_id": "workspace-b"})
        status = await self.client.get(f"/admin/account-pool/cpa-export-jobs/{response.json()['job_id']}")
        self.assertEqual(status.json()["status"], "failed")
        self.assertEqual(status.json()["error"], "CPA HTTP 401")
        self.assertIsNone(status.json()["filename"])

    async def test_changed_credentials_prevent_upload(self):
        self.authorization.save_result.return_value = False
        with self.assertLogs("app.services.account_pool_export_job", level="ERROR"):
            response = await self.client.post("/admin/account-pool/1/export-cpa", json={"workspace_id": "workspace-b"})
        self.cpa.import_member.assert_not_awaited()
        async with self.sessions() as session:
            job = await session.get(CpaExportJob, response.json()["job_id"])
            self.assertEqual(job.status, "failed")
            self.assertIn("凭据已变更", job.error)

    async def test_unknown_workspace_is_rejected_before_login(self):
        response = await self.client.post("/admin/account-pool/1/export-cpa", json={"workspace_id": "outside"})
        self.assertEqual(response.status_code, 400)
        self.authorization.login_entry.assert_not_awaited()

    async def test_endpoint_requires_admin_session(self):
        self.app.dependency_overrides.pop(require_admin)
        from starlette.middleware.sessions import SessionMiddleware
        self.app.add_middleware(SessionMiddleware, secret_key="test-session-signing-key")
        response = await self.client.post("/admin/account-pool/1/export-cpa", json={"workspace_id": "workspace-b"})
        self.assertIn(response.status_code, (401, 403))
        self.authorization.login_entry.assert_not_awaited()
