import unittest
from unittest.mock import AsyncMock, patch

from app.services.openai_automatic_login import (
    AutomaticLoginDependencies,
    AutomaticLoginRequest,
    OpenAIAutomaticLoginError,
    OpenAIAutomaticLoginService,
)


class FakeCookies:
    def __init__(self):
        self.values = {}

    def set(self, name, value, **kwargs):
        self.values[name] = value

    def get(self, name):
        return self.values.get(name)


class FakeResponse:
    def __init__(self, status_code, *, url="", headers=None, payload=None):
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}
        self.payload = payload or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, get_responses, post_responses):
        self.cookies = FakeCookies()
        self.get_responses = list(get_responses)
        self.post_responses = list(post_responses)
        self.posts = []

    async def get(self, url, **kwargs):
        response = self.get_responses.pop(0)
        response.url = response.url or url
        return response

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("json")))
        response = self.post_responses.pop(0)
        response.url = response.url or url
        return response


class OpenAIAutomaticLoginTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return AutomaticLoginRequest(
            email="member@example.com",
            password="member-password",
            totp_secret="JBSWY3DPEHPK3PXP",
            account_id="team-account",
            oauth_draft={
                "authorize_url": "https://auth.openai.com/oauth/authorize?state=test-state",
                "state": "test-state",
                "code_verifier": "test-verifier",
                "client_id": "test-client",
                "redirect_uri": "http://localhost:1455/auth/callback",
            },
            db_session=object(),
            identifier="member-auto-login-1",
        )

    async def test_password_totp_flow_exchanges_callback_code(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(302, headers={"location": "/log-in"}),
                FakeResponse(200),
                FakeResponse(200),
            ],
            post_responses=[
                FakeResponse(200, payload={"continue_url": "/log-in/password"}),
                FakeResponse(200, payload={
                    "continue_url": "/mfa-challenge/totp-factor",
                    "page": {"type": "mfa_challenge"},
                    "oai-client-auth-session": {
                        "mfa_challenge_factors": [{
                            "factor_type": "totp", "id": "totp-factor"
                        }]
                    },
                }),
                FakeResponse(200),
                FakeResponse(200, payload={
                    "continue_url": "http://localhost:1455/auth/callback?code=test-code&state=test-state"
                }),
            ],
        )
        exchange = AsyncMock(return_value={
            "success": True,
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "id",
        })
        service = OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(return_value=session),
            clear_session=AsyncMock(),
            exchange_code=exchange,
        ))

        with patch("app.services.openai_automatic_login.generate_totp", return_value="123456"):
            result = await service.login(self.request())

        self.assertTrue(result["success"])
        self.assertEqual(session.posts[-1][1]["code"], "123456")
        self.assertEqual(exchange.await_args.kwargs["code"], "test-code")
        self.assertEqual(exchange.await_args.kwargs["code_verifier"], "test-verifier")

    async def test_callback_state_mismatch_is_rejected_before_exchange(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(302, headers={"location": "/log-in"}),
                FakeResponse(200),
                FakeResponse(200),
            ],
            post_responses=[
                FakeResponse(200, payload={"continue_url": "/log-in/password"}),
                FakeResponse(200, payload={
                    "continue_url": "http://localhost:1455/auth/callback?code=test-code&state=wrong"
                }),
            ],
        )
        exchange = AsyncMock()
        service = OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(return_value=session),
            clear_session=AsyncMock(),
            exchange_code=exchange,
        ))

        with self.assertRaisesRegex(OpenAIAutomaticLoginError, "state"):
            await service.login(self.request())
        exchange.assert_not_awaited()
