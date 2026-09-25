import unittest
import json
from unittest.mock import AsyncMock, patch

from app.models import AccountPoolEntry, AccountPoolWorkspace
from app.services.encryption import encryption_service
from app.services.account_pool_usage import AccountPoolUsageService, parse_usage


class _Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _Client:
    def __init__(self, response):
        self.response = response
        self.headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, _url, headers):
        self.headers = headers
        return self.response


class AccountPoolUsageTests(unittest.IsolatedAsyncioTestCase):
    def test_team_usage_requires_matching_workspace_json(self):
        def encrypted(workspace_id):
            return encryption_service.encrypt_token(json.dumps({"accounts": [{
                "credentials": {"access_token": f"token-{workspace_id}",
                                "chatgpt_account_id": workspace_id}
            }]}))

        entry = AccountPoolEntry(workspace_id="team-a", export_json_encrypted=encrypted("team-a"))
        workspace = AccountPoolWorkspace(workspace_id="team-b",
                                         export_json_encrypted=encrypted("team-b"))
        self.assertIsNone(AccountPoolUsageService._entry_token(entry, "team-b"))
        self.assertEqual(AccountPoolUsageService._entry_token(entry, "team-b", workspace),
                         ("token-team-b", "team-b"))

    def test_parse_usage_supports_aliases_and_derived_remaining(self):
        result = parse_usage({
            "rate_limits": {
                "300min": {"used": 20, "limit": 100, "reset_at": "short-reset"},
                "7d": {"remaining": 0, "limit": 500, "resets_at": "weekly-reset"},
            }
        })
        self.assertEqual(result["5h"]["remaining"], 80)
        self.assertEqual(result["5h"]["reset_at"], "short-reset")
        self.assertEqual(result["1week"]["state"], "exhausted")
        self.assertEqual(result["1week"]["used"], 500)

    def test_parse_real_wham_windows_by_duration(self):
        usage = parse_usage({"rate_limit": {
            "primary_window": {
                "limit_window_seconds": 18000, "used_percent": 25.5,
                "reset_at": 1790400000,
            },
            "secondary_window": {
                "limit_window_seconds": 604800, "used_percent": 100,
                "reset_at": 1791000000,
            },
        }})
        self.assertEqual(usage["5h"]["remaining"], 74.5)
        self.assertEqual(usage["5h"]["reset_at"], "1790400000")
        self.assertEqual(usage["1week"]["state"], "exhausted")
        self.assertEqual(usage["1week"]["reset_at"], "1791000000")

    def test_weekly_only_response_does_not_invent_five_hour_quota(self):
        usage = parse_usage({"rate_limit": {
            "primary_window": {
                "limit_window_seconds": 604800, "used_percent": 0,
                "reset_at": 1791000000,
            },
            "secondary_window": None,
        }})
        self.assertIsNone(usage["5h"])
        self.assertEqual(usage["1week"]["remaining"], 100)

    def test_unrelated_or_empty_windows_are_not_valid_quota(self):
        self.assertIsNone(parse_usage({"rate_limit": {"primary_window": {
            "limit_window_seconds": 2592000, "used_percent": 10,
        }}}))
        self.assertIsNone(parse_usage({"rate_limits": {"7d": {"reset_at": 1791000000}}}))

    async def test_only_401_is_invalid(self):
        invalid = AccountPoolUsageService(
            client_factory=lambda **_: _Client(_Response(401)),
        )
        forbidden = AccountPoolUsageService(
            client_factory=lambda **_: _Client(_Response(403)),
        )
        self.assertEqual((await invalid._check_token(("token", "account")))["status"], "invalid")
        self.assertEqual((await forbidden._check_token(("token", "account")))["status"], "unknown")

    async def test_request_uses_browser_fingerprint_account_and_proxy(self):
        client = _Client(_Response(200, {"rate_limit": {"primary_window": {
            "limit_window_seconds": 604800, "used_percent": 10,
            "reset_at": 1791000000,
        }}}))
        factory = unittest.mock.MagicMock(return_value=client)
        service = AccountPoolUsageService(client_factory=factory)
        result = await service._check_token(
            ("token", "account"), {"all": "http://proxy.example:8080"},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["1week"]["remaining"], 90)
        self.assertEqual(client.headers["Chatgpt-Account-Id"], "account")
        self.assertEqual(client.headers["Authorization"], "Bearer token")
        self.assertEqual(factory.call_args.kwargs["impersonate"], "chrome110")
        self.assertEqual(factory.call_args.kwargs["proxies"]["all"],
                         "http://proxy.example:8080")

    async def test_check_many_reads_member_authorizations_for_missing_pool_tokens(self):
        service = AccountPoolUsageService()
        service._check_token = AsyncMock(return_value={"status": "unavailable"})
        scalars = unittest.mock.MagicMock()
        scalars.all.return_value = []
        result = unittest.mock.MagicMock()
        result.scalars.return_value = scalars
        db = unittest.mock.MagicMock()
        db.execute = AsyncMock(return_value=result)

        with patch.object(service, "_proxies", new=AsyncMock(return_value=None)):
            values = await service.check_many(
                db,
                [("one@example.com", "space-a"), ("two@example.com", "space-b")],
            )

        self.assertEqual(db.execute.await_count, 2)
        self.assertEqual(len(values), 2)
        self.assertEqual(service._check_token.await_count, 2)


if __name__ == "__main__":
    unittest.main()
