import unittest
import base64
import json
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
    def __init__(self, get_responses, post_responses, auth_claims=None):
        self.cookies = FakeCookies()
        if auth_claims is not None:
            payload = base64.urlsafe_b64encode(
                json.dumps(auth_claims).encode()
            ).decode().rstrip("=")
            self.cookies.set("oai-client-auth-session", f"header.{payload}.signature")
        self.get_responses = list(get_responses)
        self.post_responses = list(post_responses)
        self.posts = []
        self.post_headers = []

    async def get(self, url, **kwargs):
        response = self.get_responses.pop(0)
        response.url = response.url or url
        return response

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("json")))
        self.post_headers.append(kwargs.get("headers") or {})
        response = self.post_responses.pop(0)
        response.url = response.url or url
        return response


class OpenAIAutomaticLoginTests(unittest.IsolatedAsyncioTestCase):
    def request(self, account_id="team-account"):
        return AutomaticLoginRequest(
            email="member@example.com",
            password="member-password",
            totp_secret="JBSWY3DPEHPK3PXP",
            account_id=account_id,
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
            issue_sentinel=AsyncMock(return_value="sentinel-token"),
        ))

        with patch("app.services.openai_automatic_login.generate_totp", return_value="123456"):
            result = await service.login(self.request())

        self.assertTrue(result["success"])
        self.assertEqual(result["verified_totp_factor_id"], "totp-factor")
        self.assertEqual(session.posts[-1][1]["code"], "123456")
        self.assertTrue(all(
            headers.get("openai-sentinel-token") == "sentinel-token"
            for headers in session.post_headers
        ))
        self.assertEqual(exchange.await_args.kwargs["code"], "test-code")
        self.assertEqual(exchange.await_args.kwargs["code_verifier"], "test-verifier")

    async def test_verify_credentials_stops_before_workspace_and_closes_session(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(302, headers={"location": "/log-in"}),
                FakeResponse(200), FakeResponse(200), FakeResponse(200),
                FakeResponse(200),
            ],
            post_responses=[
                FakeResponse(200, payload={"continue_url": "/log-in/password"}),
                FakeResponse(200, payload={"continue_url": "/workspace"}),
            ],
        )
        clear_session = AsyncMock()
        exchange = AsyncMock()
        service = OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(return_value=session),
            clear_session=clear_session,
            exchange_code=exchange,
            issue_sentinel=AsyncMock(return_value="sentinel-token"),
        ))

        await service.verify_credentials(self.request())

        self.assertEqual(len(session.posts), 2)
        self.assertEqual(clear_session.await_count, 2)
        exchange.assert_not_awaited()

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
            issue_sentinel=AsyncMock(return_value="sentinel-token"),
        ))

        with self.assertRaisesRegex(OpenAIAutomaticLoginError, "state"):
            await service.login(self.request())
        exchange.assert_not_awaited()

    async def test_invalid_password_error_is_actionable_and_redacted(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(302, headers={"location": "/log-in"}),
                FakeResponse(200),
                FakeResponse(200),
            ],
            post_responses=[
                FakeResponse(200, payload={"continue_url": "/log-in/password"}),
                FakeResponse(401, payload={"error": {
                    "code": "invalid_username_or_password",
                    "type": "invalid_request_error",
                    "message": "password=member-password token=secret-token",
                }}),
            ],
        )
        service = OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(return_value=session),
            clear_session=AsyncMock(),
            exchange_code=AsyncMock(),
            issue_sentinel=AsyncMock(return_value="sentinel-token"),
        ))

        with self.assertLogs("app.services.openai_auth_errors", level="WARNING") as logs:
            with self.assertRaisesRegex(
                OpenAIAutomaticLoginError, "密码错误、账号停用或封禁"
            ) as raised:
                await service.login(self.request())

        output = " ".join(logs.output) + str(raised.exception)
        self.assertIn("invalid_username_or_password", output)
        self.assertNotIn("member-password", output)
        self.assertNotIn("secret-token", output)

    async def test_workspace_selection_relies_on_upstream_response(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(302, headers={"location": "/log-in"}),
                FakeResponse(200),
                FakeResponse(200),
                FakeResponse(200),
            ],
            post_responses=[
                FakeResponse(200, payload={"continue_url": "/log-in/password"}),
                FakeResponse(200, payload={"continue_url": "/workspace"}),
                FakeResponse(200, payload={
                    "continue_url": (
                        "http://localhost:1455/auth/callback?"
                        "code=test-code&state=test-state"
                    )
                }),
            ],
        )
        exchange = AsyncMock(return_value={"success": True})
        service = OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(return_value=session),
            clear_session=AsyncMock(),
            exchange_code=exchange,
            issue_sentinel=AsyncMock(return_value="sentinel-token"),
        ))

        result = await service.login(self.request())

        self.assertTrue(result["success"])
        self.assertEqual(session.posts[-1][1], {"workspace_id": "team-account"})

    async def test_workspace_is_auto_selected_from_default_auth_claim(self):
        token_payload = base64.urlsafe_b64encode(json.dumps({
            "https://api.openai.com/auth": {"chatgpt_account_id": "personal-account"}
        }).encode()).decode().rstrip("=")
        personal_token = f"header.{token_payload}.signature"
        session = FakeSession(
            get_responses=[
                FakeResponse(302, headers={"location": "/log-in"}),
                FakeResponse(200),
                FakeResponse(200),
                FakeResponse(200),
            ],
            post_responses=[
                FakeResponse(200, payload={"continue_url": "/log-in/password"}),
                FakeResponse(200, payload={"continue_url": "/workspace"}),
                FakeResponse(200, payload={
                    "continue_url": (
                        "http://localhost:1455/auth/callback?"
                        "code=test-code&state=test-state"
                    )
                }),
            ],
            auth_claims={
                "organizations": [{
                    "id": "external-workspace",
                    "title": "External Workspace",
                    "is_default": True,
                }],
            },
        )
        service = OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(return_value=session),
            clear_session=AsyncMock(),
            exchange_code=AsyncMock(return_value={
                "success": True,
                "access_token": personal_token,
            }),
            issue_sentinel=AsyncMock(return_value="sentinel-token"),
        ))

        result = await service.login(self.request(account_id=""))

        self.assertEqual(session.posts[-1][1], {"workspace_id": "external-workspace"})
        self.assertEqual(result["workspace"]["workspace_id"], "external-workspace")

    async def test_callback_without_workspace_can_complete_login(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(302, headers={"location": "/log-in"}),
                FakeResponse(200),
                FakeResponse(200),
            ],
            post_responses=[
                FakeResponse(200, payload={"continue_url": "/log-in/password"}),
                FakeResponse(200, payload={
                    "continue_url": (
                        "http://localhost:1455/auth/callback?"
                        "code=test-code&state=test-state"
                    )
                }),
            ],
            auth_claims={"email": "member@example.com"},
        )
        service = OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(return_value=session),
            clear_session=AsyncMock(),
            exchange_code=AsyncMock(return_value={"success": True}),
            issue_sentinel=AsyncMock(return_value="sentinel-token"),
        ))

        result = await service.login(self.request(account_id=""))

        self.assertEqual(len(session.posts), 2)
        self.assertEqual(result["workspace"]["status"], "no_workspace")
