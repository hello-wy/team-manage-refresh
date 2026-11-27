"""CPA format and HTTP upload contract; no live credentials are used."""
import base64
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.services.openai_oauth_clients import BPS_CLIENT_ID
from app.services.cpa import CpaExportError, CpaService, build_auth_file


def jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "header." + encoded + ".signature"


def export_payload(workspace="workspace-a"):

    return {
        "exported_at": "2030-01-01T00:00:00Z", "type": "sub2api-data",
        "accounts": [{"platform": "openai", "type": "oauth", "group_ids": [42],
                      "credentials": {
                          "email": "member@example.com", "chatgpt_account_id": workspace,
                          "access_token": jwt({"client_id": BPS_CLIENT_ID, "exp": 1893459600,
                                               "sub": "test-subject", "https://api.openai.com/auth": {"chatgpt_account_id": workspace}}),
                          "refresh_token": "test-refresh",
                          "id_token": jwt({"email": "member@example.com"}), "client_id": BPS_CLIENT_ID,
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

    async def test_uploads_flat_bps_file_using_management_auth(self):
        result = await self.service.import_member(export_payload(), None)
        request = self.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/v0/management/auth-files")
        self.assertEqual(request.headers["Authorization"], "Bearer test-management-key")
        self.assertEqual(request.url.params["name"], result["filename"])
        uploaded = json.loads(request.content)
        self.assertEqual(uploaded["type"], "bps")
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
        second = export_payload("workspace-b")
        self.assertNotEqual(filename, build_auth_file(second)[0])
        self.assertRegex(filename, r"^bps-[0-9a-f]{32}\.json$")
        second["accounts"][0]["credentials"]["email"] = "../../member@example.com"
        with self.assertRaisesRegex(CpaExportError, "邮箱不一致"):
            build_auth_file(second)

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

    def test_codex_tokens_cannot_be_relabelled_as_bps(self):
        for declared, actual in (("codex-client", BPS_CLIENT_ID), (BPS_CLIENT_ID, "codex-client")):
            payload = export_payload()
            credentials = payload["accounts"][0]["credentials"]
            credentials["client_id"] = declared
            credentials["access_token"] = jwt({"client_id": actual})
            with self.assertRaisesRegex(CpaExportError, "独立的 BPS"):
                build_auth_file(payload)

    def test_wrong_workspace_and_expired_token_are_rejected(self):
        payload = export_payload()
        payload["accounts"][0]["credentials"]["chatgpt_account_id"] = "other-workspace"
        with self.assertRaisesRegex(CpaExportError, "工作区不一致"):
            build_auth_file(payload)
        payload = export_payload()
        payload["accounts"][0]["credentials"]["expires_at"] = "2020-01-01T00:00:00Z"
        with self.assertRaisesRegex(CpaExportError, "已过期"):
            build_auth_file(payload)
