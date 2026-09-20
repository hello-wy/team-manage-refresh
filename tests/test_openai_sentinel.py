import json
import unittest

from app.services.openai_sentinel import (
    OpenAISentinelError,
    SENTINEL_FLOW,
    issue_sentinel_token,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.request = None

    async def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return self.response


class OpenAISentinelTests(unittest.IsolatedAsyncioTestCase):
    async def test_issues_device_bound_token(self):
        session = FakeSession(FakeResponse(200, {
            "p": "requirements",
            "token": "challenge-token",
            "proofofwork": {"required": False},
        }))

        token = json.loads(await issue_sentinel_token(session, "device-id"))

        self.assertEqual(token["id"], "device-id")
        self.assertEqual(token["flow"], SENTINEL_FLOW)
        self.assertEqual(token["c"], "challenge-token")
        self.assertNotIn("challenge-token", str(session.request[1]["headers"]))

    async def test_http_failure_is_explicit(self):
        session = FakeSession(FakeResponse(403, {}))

        with self.assertRaisesRegex(OpenAISentinelError, "sentinel_http_403"):
            await issue_sentinel_token(session, "device-id")
