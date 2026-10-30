"""CPA format and HTTP upload contract; no live credentials are used."""
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.services.cpa import CpaExportError, CpaService, build_auth_file


def export_payload():
    return {
        "exported_at": "2030-01-01T00:00:00Z", "type": "sub2api-data",
        "accounts": [{"platform": "openai", "type": "oauth", "group_ids": [42],
                      "credentials": {
                          "email": "member@example.com", "chatgpt_account_id": "workspace-a",
                          "access_token": "test-access", "refresh_token": "test-refresh",
                          "id_token": "test-id", "client_id": "test-client",
                          "expires_at": "2030-01-01T01:00:00Z", "plan_type": "team",
                      }}],
    }


class CpaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(base_url="https://cpa.test/", api_key="test-management-key", proxy=None)
        self.load_config = AsyncMock(return_value=self.config)
        self.requests = []
        self.status = 200
        self.failure = None
        self.service = CpaService(load_config=self.load_config, client_factory=self.client)

    def client(self, **options):
        self.client_options = options
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), **options)

    def handle(self, request):
        self.requests.append(request)
        if self.failure:
            raise self.failure
        return httpx.Response(self.status, json={"status": "ok" if self.status == 200 else "error"})

    async def test_uploads_flat_codex_file_using_management_auth(self):
        result = await self.service.import_member(export_payload(), None)
        request = self.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/v0/management/auth-files")
        self.assertEqual(request.headers["Authorization"], "Bearer test-management-key")
        self.assertEqual(request.url.params["name"], result["filename"])
        uploaded = json.loads(request.content)
        self.assertEqual(uploaded["type"], "codex")
        self.assertEqual(uploaded["account_id"], "workspace-a")
        self.assertEqual(uploaded["expired"], "2030-01-01T01:00:00Z")
        self.assertEqual(uploaded["refresh_token"], "test-refresh")
        self.assertNotIn("accounts", uploaded)
        self.assertNotIn("group_ids", uploaded)
        self.assertNotIn("test-refresh", json.dumps(result))

    def test_workspaces_have_distinct_stable_safe_filenames_and_payload_is_immutable(self):
        first = export_payload()
        original = deepcopy(first)
        filename, _ = build_auth_file(first)
        self.assertEqual(first, original)
        second = deepcopy(first)
        second["accounts"][0]["credentials"]["chatgpt_account_id"] = "workspace-b"
        self.assertNotEqual(filename, build_auth_file(second)[0])
        second["accounts"][0]["credentials"]["email"] = "../../member@example.com"
        self.assertNotIn("/", build_auth_file(second)[0])

    async def test_missing_refresh_token_does_not_upload_incomplete_credentials(self):
        payload = export_payload()
        del payload["accounts"][0]["credentials"]["refresh_token"]
        with self.assertRaisesRegex(CpaExportError, "refresh_token"):
            await self.service.import_member(payload, None)
        self.assertEqual(self.requests, [])

    async def test_missing_config_is_explicit(self):
        self.load_config.return_value = None
        with self.assertRaisesRegex(CpaExportError, "CliproxyAPI"):
            await self.service.import_member(export_payload(), None)
        self.assertEqual(self.requests, [])

    async def test_http_failure_is_reported_without_retry(self):
        self.status = 401
        with self.assertRaisesRegex(CpaExportError, "HTTP 401"):
            await self.service.import_member(export_payload(), None)
        self.assertEqual(len(self.requests), 1)

    async def test_network_failure_exposes_uncertainty_without_retry(self):
        self.failure = httpx.ReadTimeout("connection interrupted")
        with self.assertRaisesRegex(CpaExportError, "上传结果未确认"):
            await self.service.import_member(export_payload(), None)
        self.assertEqual(len(self.requests), 1)
