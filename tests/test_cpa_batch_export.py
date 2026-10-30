"""Batch CPA routes preserve per-Team outcomes and continue after failures."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.database import get_db
from app.dependencies.auth import require_admin
from app.routes import cpa_export
from app.services.cpa import CpaExportError


class CpaBatchExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = SimpleNamespace(rollback=AsyncMock())
        self.failures = {}
        self.service = SimpleNamespace(push=AsyncMock(side_effect=self.export))
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key="test-session-signing-key")
        self.app.include_router(cpa_export.router)
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        self.app.dependency_overrides[cpa_export.get_team_export_service] = lambda: self.service
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def export(self, team_id, db):
        if team_id in self.failures:
            raise self.failures[team_id]
        return {"team_id": team_id, "filename": f"team-{team_id}.json"}

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_exports_only_selected_teams_once(self):
        response = await self.client.post("/admin/teams/batch-push-cpa", json={"ids": [2, 5, 2]})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual([item["team_id"] for item in result["results"]], [2, 5])
        self.assertEqual([call.args[0] for call in self.service.push.await_args_list], [2, 5])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_first_team_failure_does_not_prevent_remaining_exports(self):
        self.failures[2] = CpaExportError("CPA 上传结果未确认")
        response = await self.client.post("/admin/teams/batch-push-cpa", json={"ids": [2, 5, 8]})
        result = response.json()
        self.assertTrue(result["success"])
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["results"][0]["error"], "CPA 上传结果未确认")
        self.assertEqual(self.service.push.await_count, 3)
        self.db.rollback.assert_awaited_once()

    async def test_all_failures_return_error_with_each_team_reason(self):
        self.failures.update({2: CpaExportError("密码缺失"), 5: CpaExportError("CPA HTTP 401")})
        response = await self.client.post("/admin/teams/batch-push-cpa", json={"ids": [2, 5]})
        self.assertEqual(response.status_code, 400)
        result = response.json()
        self.assertFalse(result["success"])
        self.assertEqual(result["success_count"], 0)
        self.assertIn("Team 2: 密码缺失", result["error"])
        self.assertIn("Team 5: CPA HTTP 401", result["error"])

    async def test_unexpected_failure_is_logged_and_other_team_still_exports(self):
        self.failures[2] = RuntimeError("unexpected failure")
        with self.assertLogs("app.routes.cpa_export", level="ERROR"):
            response = await self.client.post("/admin/teams/batch-push-cpa", json={"ids": [2, 5]})
        self.assertEqual(response.json()["success_count"], 1)
        self.assertFalse(response.json()["results"][0]["success"])

    async def test_invalid_selection_does_not_export(self):
        for ids in ([], [0], [-1], ["invalid"]):
            response = await self.client.post("/admin/teams/batch-push-cpa", json={"ids": ids})
            self.assertEqual(response.status_code, 422)
        self.service.push.assert_not_awaited()

    async def test_admin_session_is_required(self):
        self.app.dependency_overrides.pop(require_admin)
        response = await self.client.post("/admin/teams/batch-push-cpa", json={"ids": [2, 5]})
        self.assertEqual(response.status_code, 401)
        self.service.push.assert_not_awaited()
