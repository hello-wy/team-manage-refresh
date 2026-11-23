import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from app.services.openai_automatic_login import (
    AutomaticLoginDependencies, AutomaticLoginRequest, OpenAIAutomaticLoginService,
)
from test_account_pool_subscription import tokens
from test_openai_automatic_login import FakeSession

CALLBACK = "http://localhost:1455/auth/callback?code=code&state=test-state"
SUBSCRIPTION_QUERY = "app.services.openai_automatic_login.query_personal_subscription"


class LoginSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = FakeSession([], [])
        self.clear = AsyncMock()
        self.exchange = AsyncMock(return_value={"success": True, **tokens("plus")})
        self.service = OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(return_value=self.session), clear_session=self.clear,
            exchange_code=self.exchange, issue_sentinel=AsyncMock(return_value="sentinel"),
        ))
        self.request = AutomaticLoginRequest(
            email="personal@example.com", password="password", totp_secret="secret",
            account_id="", oauth_draft={
                "authorize_url": "https://auth.openai.com/oauth/authorize",
                "state": "test-state", "client_id": "client",
                "code_verifier": "verifier", "redirect_uri": "http://localhost:1455/auth/callback",
            }, db_session=None, identifier="test-subscription", read_subscription=True,
        )

    async def test_known_personal_workspace_is_queried_without_a_plan_claim(self):
        result = tokens("")
        with patch(SUBSCRIPTION_QUERY, new=AsyncMock(return_value="Pro100")) as query:
            self.assertEqual(await self.service._personal_subscription(self.session, result), "Pro100")
        query.assert_awaited_once_with(self.session, result["access_token"], "personal")

    async def test_team_access_token_does_not_inherit_personal_id_token(self):
        result = tokens("team", entitlement={"plan_type": "free"})
        result["id_token"] = tokens("promax")["access_token"]
        with patch(SUBSCRIPTION_QUERY, new=AsyncMock()) as query:
            self.assertEqual(await self.service._personal_subscription(self.session, result), "")
        query.assert_not_awaited()

    async def test_workspace_mismatch_does_not_query_or_assign_plan(self):
        result = tokens("plus")
        result["workspace"]["workspace_id"] = "another-personal"
        with patch(SUBSCRIPTION_QUERY, new=AsyncMock()) as query:
            self.assertEqual(await self.service._personal_subscription(self.session, result), "")
        query.assert_not_awaited()

    async def test_failed_query_retains_token_plan(self):
        with patch(SUBSCRIPTION_QUERY, new=AsyncMock(return_value="")):
            result = await self.service._personal_subscription(self.session, tokens("prolite"))
        self.assertEqual(result, "Pro100")

    def prepare_login(self, url=CALLBACK):
        self.service._follow = AsyncMock(return_value=(None, "https://auth.openai.com/log-in"))
        self.service._submit_email = AsyncMock(return_value="https://auth.openai.com/log-in/password")
        self.service._submit_password_and_totp = AsyncMock(return_value=(url, "factor"))
        self.service._select_workspace = AsyncMock(return_value=(CALLBACK, {
            "workspace_id": "personal", "is_personal": True,
        }))

    async def test_login_reads_plan_once_in_the_same_proxy_session(self):
        self.prepare_login()
        with patch(SUBSCRIPTION_QUERY, new=AsyncMock(return_value="Pro500")) as query:
            result = await self.service.login(self.request)
        self.assertEqual(result["subscription"], {"personal_plan": "Pro500"})
        self.assertIs(query.await_args.args[0], self.session)
        self.exchange.assert_awaited_once()
        self.service._dependencies.get_session.assert_awaited_once_with(
            self.request.db_session, self.request.identifier, email=self.request.email,
        )

    async def test_liveness_selects_only_personal_and_returns_no_tokens(self):
        self.prepare_login("https://auth.openai.com/workspace")
        self.exchange.return_value = {"success": True, **tokens("")}
        scan = {"workspace_id": "team", "available_workspaces": [
            {"id": "team", "is_personal": False},
            {"id": "personal", "is_personal": True},
        ]}
        with patch("app.services.openai_automatic_login.discover_workspaces",
                   new=AsyncMock(return_value=scan)), \
                patch(SUBSCRIPTION_QUERY, new=AsyncMock(return_value="Pro200")) as query:
            result = await self.service.verify_credentials(self.request)
        self.assertEqual(result["workspace_id"], "team")
        self.assertEqual(result["personal_plan"], "Pro200")
        self.assertEqual(result["available_workspaces"][1]["personal_plan"], "Pro200")
        self.assertNotIn("personal_plan", result["available_workspaces"][0])
        self.assertNotIn("access_token", result)
        self.assertIs(query.await_args.args[0], self.session)
        self.assertEqual(self.clear.await_count, 2)
        self.service._select_workspace.assert_awaited_once_with(
            self.session, "https://auth.openai.com/workspace", "personal",
            unittest.mock.ANY, "sentinel",
        )
        self.exchange.assert_awaited_once()

    async def test_liveness_default_does_not_exchange_tokens(self):
        self.prepare_login("https://auth.openai.com/workspace")
        scan = {"available_workspaces": [{"id": "personal", "is_personal": True}]}
        with patch("app.services.openai_automatic_login.discover_workspaces",
                   new=AsyncMock(return_value=scan)):
            await self.service.verify_credentials(replace(self.request, read_subscription=False))
        self.exchange.assert_not_awaited()
        self.service._select_workspace.assert_not_awaited()

    async def test_subscription_exchange_failure_does_not_fail_liveness(self):
        self.prepare_login()
        self.exchange.side_effect = ValueError("exchange failed")
        scan = {"available_workspaces": [{"id": "personal", "is_personal": True}]}
        with patch("app.services.openai_automatic_login.discover_workspaces",
                   new=AsyncMock(return_value=scan)):
            result = await self.service.verify_credentials(self.request)
        self.assertEqual(result, scan)
        self.assertNotIn("personal_plan", result)
        self.assertEqual(self.clear.await_count, 2)

    async def test_callback_updates_only_the_matching_personal_workspace(self):
        self.prepare_login()
        scan = {"workspace_id": "other", "available_workspaces": [
            {"id": "other", "is_personal": True}, {"id": "personal", "is_personal": True},
        ]}
        with patch("app.services.openai_automatic_login.discover_workspaces",
                   new=AsyncMock(return_value=scan)), \
                patch(SUBSCRIPTION_QUERY, new=AsyncMock(return_value="Plus")):
            result = await self.service.verify_credentials(self.request)
        self.assertNotIn("personal_plan", result["available_workspaces"][0])
        self.assertEqual(result["available_workspaces"][1]["personal_plan"], "Plus")
