import unittest
from unittest.mock import AsyncMock

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

    async def test_only_401_is_invalid(self):
        invalid = AccountPoolUsageService(
            client_factory=lambda **_: _Client(_Response(401)),
        )
        forbidden = AccountPoolUsageService(
            client_factory=lambda **_: _Client(_Response(403)),
        )
        self.assertEqual((await invalid._check_token(("token", "account")))["status"], "invalid")
        self.assertEqual((await forbidden._check_token(("token", "account")))["status"], "unknown")

    async def test_check_many_does_single_database_read_before_network(self):
        service = AccountPoolUsageService()
        service._check_token = AsyncMock(return_value={"status": "unavailable"})
        scalars = unittest.mock.MagicMock()
        scalars.all.return_value = []
        result = unittest.mock.MagicMock()
        result.scalars.return_value = scalars
        db = unittest.mock.MagicMock()
        db.execute = AsyncMock(return_value=result)

        values = await service.check_many(
            db,
            [("one@example.com", "space-a"), ("two@example.com", "space-b")],
        )

        self.assertEqual(db.execute.await_count, 1)
        self.assertEqual(len(values), 2)
        self.assertEqual(service._check_token.await_count, 2)


if __name__ == "__main__":
    unittest.main()
