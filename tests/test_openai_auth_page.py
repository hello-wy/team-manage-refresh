import base64
import json
import unittest
from unittest.mock import AsyncMock

from app.services.openai_auth_protocol import inspect_auth_page_workspace
from app.services.openai_automatic_login import (
    AutomaticLoginDependencies,
    AutomaticLoginRequest,
    OpenAIAutomaticLoginService,
    OpenAIAutomaticLoginError,
)
from app.services.openai_workspace_discovery import discover_workspaces


class FakeCookies:
    def __init__(self):
        self.values = {"oai-client-auth-session": "opaque-session-cookie"}

    def set(self, name, value, **_kwargs):
        self.values[name] = value

    def get(self, name):
        return self.values.get(name)


class FakeResponse:
    def __init__(self, status_code, *, headers=None, payload=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.payload = payload or {}
        self.text = text
        self.url = ""

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, get_responses, post_responses):
        self.cookies = FakeCookies()
        self.get_responses = list(get_responses)
        self.post_responses = list(post_responses)
        self.posts = []

    async def get(self, url, **_kwargs):
        response = self.get_responses.pop(0)
        response.url = url
        return response

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("json")))
        response = self.post_responses.pop(0)
        response.url = url
        return response


def auth_page_html():
    flattened = [
        {"_1": 2}, "loaderData", {"_3": 4}, "route", {"_5": 6}, "session",
        {"_7": 8}, "workspaces", [9, 16],
        {"_10": 11, "_12": 13, "_14": 15},
        "id", "team-a", "name", "Team A",
        "kind", "organization",
        {"_10": 17, "_12": -1, "_14": 18}, "personal-a", "personal",
    ]
    serialized = json.dumps(flattened)
    return f"<script>window.__reactRouterContext.streamController.enqueue({json.dumps(serialized)})</script>"


class OpenAIAuthPageTests(unittest.IsolatedAsyncioTestCase):
    def test_workspace_scan_includes_all_auth_page_stream_documents(self):
        personal = json.dumps({"workspaces": [{"id": "personal-a", "kind": "personal"}]})
        prefix = f'<script>streamController.enqueue({json.dumps(personal)})</script>'
        scan = inspect_auth_page_workspace(prefix + auth_page_html())
        self.assertEqual({workspace["id"] for workspace in scan["available_workspaces"]},
                         {"personal-a", "team-a"})

    async def test_personal_cookie_does_not_skip_team_discovery_from_page(self):
        session = FakeSession([FakeResponse(200, text=auth_page_html())], [])
        claims = {"workspaces": [{"id": "personal-a", "kind": "personal"}]}
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        session.cookies.set("oai-client-auth-session", f"header.{encoded}.signature")
        scan = await discover_workspaces(
            session, "https://auth.openai.com/workspace", device_id="test-device",
            sentinel_token="test-sentinel", expected_state="test-state",
        )
        self.assertEqual({item["id"] for item in scan["available_workspaces"]}, {"personal-a", "team-a"})
        self.assertEqual(session.get_responses, [])
        self.assertEqual(session.posts, [])

    async def test_unreadable_auth_page_is_reported_instead_of_claiming_personal_account(self):
        for response in (FakeResponse(403), FakeResponse(200, text="Unknown page format")):
            with self.subTest(status=response.status_code):
                session = FakeSession([response], [])
                with self.assertRaises(OpenAIAutomaticLoginError):
                    await discover_workspaces(
                        session, "https://auth.openai.com/workspace", device_id="test-device",
                        sentinel_token="test-sentinel", expected_state="test-state",
                    )

    def test_workspace_data_is_decoded_from_react_router_stream(self):
        scan = inspect_auth_page_workspace(auth_page_html())

        self.assertEqual(len(scan["available_workspaces"]), 2)
        self.assertEqual(scan["available_workspaces"][0]["id"], "team-a")
        self.assertTrue(scan["available_workspaces"][1]["is_personal"])

    async def test_login_selects_unique_organization_from_consent_page(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(302, headers={"location": "/log-in"}),
                FakeResponse(200),
                FakeResponse(200),
                FakeResponse(200),
                FakeResponse(200, text=auth_page_html()),
            ],
            post_responses=[
                FakeResponse(200, payload={"continue_url": "/log-in/password"}),
                FakeResponse(200, payload={"continue_url": "/sign-in-with-chatgpt/codex/consent"}),
                FakeResponse(200, payload={
                    "continue_url": (
                        "http://localhost:1455/auth/callback?"
                        "code=test-code&state=test-state"
                    ),
                }),
            ],
        )
        service = OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(return_value=session),
            clear_session=AsyncMock(),
            exchange_code=AsyncMock(return_value={"success": True}),
            issue_sentinel=AsyncMock(return_value="sentinel-token"),
        ))

        result = await service.login(AutomaticLoginRequest(
            email="member@example.com",
            password="member-password",
            totp_secret="JBSWY3DPEHPK3PXP",
            account_id="",
            oauth_draft={
                "authorize_url": "https://auth.openai.com/oauth/authorize?state=test-state",
                "state": "test-state",
                "code_verifier": "test-verifier",
                "client_id": "test-client",
                "redirect_uri": "http://localhost:1455/auth/callback",
            },
            db_session=object(),
            identifier="account-pool-login-1",
        ))

        self.assertEqual(session.posts[-1][1], {"workspace_id": "team-a"})
        self.assertEqual(result["workspace"]["workspace_id"], "team-a")
        self.assertEqual(result["workspace"]["workspace_name"], "Team A")


if __name__ == "__main__":
    unittest.main()
