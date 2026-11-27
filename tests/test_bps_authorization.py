"""Independent BPS grants, callback boundaries and owner credential isolation."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.database import Base
from app.models import AccountPoolEntry, Team
from app.services.bps_authorization import BpsAccountPoolAuthorizationService, TeamBpsExportPreparation
from app.services.account_pool_authorization_models import AccountPoolAuthorizationError, AccountPoolLoginResult
from app.services.chatgpt import ChatGPTService
from app.services.cpa import build_auth_file
from app.services.openai_auth_protocol import is_callback
from app.services.openai_oauth_clients import BPS_CLIENT_ID, BPS_REDIRECT_URI, BPS_SCOPE
from app.services.openai_automatic_login import AutomaticLoginDependencies, OpenAIAutomaticLoginService, OpenAIAutomaticLoginError
from test_cpa import export_payload


class BpsAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.authorization = BpsAccountPoolAuthorizationService(None, None, ChatGPTService(), team_service=None)
        self.request = self.authorization._request(
            SimpleNamespace(id=7, email="member@example.com"),
            {"password": "test-password", "two_factor_secret": "JBSWY3DPEHPK3PXP"}, "workspace-a", None)

    def test_uses_bps_client_scope_and_callback_without_codex_flags(self):
        query = parse_qs(urlparse(self.request.oauth_draft["authorize_url"]).query)
        self.assertEqual(query["client_id"], [BPS_CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [BPS_REDIRECT_URI])
        self.assertEqual(query["scope"], [BPS_SCOPE])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("codex_cli_simplified_flow", query)
        self.assertNotIn("id_token_add_organizations", query)
        self.assertFalse(self.request.read_subscription)
        self.assertEqual(self.request.identifier, "account-pool-bps-7")

    def test_result_preserves_real_bps_client_and_selected_workspace(self):
        credentials = export_payload()["accounts"][0]["credentials"]
        result = self.authorization._result("member@example.com", {
            **credentials, "workspace": {"workspace_id": "workspace-a", "status": "workspace_ok"}}, "workspace-a")
        name, auth = build_auth_file(result.payload)
        self.assertTrue(name.startswith("bps-"))
        self.assertEqual(auth["client_id"], BPS_CLIENT_ID)
        self.assertEqual(auth["account_id"], "workspace-a")

    async def test_bps_result_cannot_be_saved_in_codex_cache(self):
        with self.assertRaisesRegex(AccountPoolAuthorizationError, "不能写入 Codex"):
            await self.authorization.save_result(None, 7, None)

    async def test_bps_callback_stops_redirects_and_is_exchanged_only_for_expected_address(self):
        exchange = AsyncMock(return_value={"success": True})
        login = OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(), clear_session=AsyncMock(), exchange_code=exchange))
        state = self.request.oauth_draft["state"]
        callback = BPS_REDIRECT_URI + "?code=test-code&state=" + state
        self.assertTrue(is_callback(callback))
        self.assertFalse(is_callback(callback.replace("bps.openai.com", "example.com")))
        session = SimpleNamespace(get=AsyncMock())
        _, result_url = await login._follow(session, callback, "device", "sentinel")
        session.get.assert_not_awaited()
        self.assertEqual(result_url, callback)
        await login._exchange(self.request, callback)
        self.assertEqual(exchange.await_args.kwargs["client_id"], BPS_CLIENT_ID)
        for invalid in (callback.replace("bps.openai.com", "example.com"), callback + "&code=second",
                        callback.replace(state, "wrong-state")):
            with self.assertRaises(OpenAIAutomaticLoginError):
                await login._exchange(self.request, invalid)
        self.assertEqual(exchange.await_count, 1)

    async def test_team_export_obtains_new_bps_grant_and_preserves_all_codex_credentials(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                team = Team(email="member@example.com", account_id="workspace-a", access_token_encrypted="team-codex")
                entry = AccountPoolEntry(email=team.email, password_encrypted="password", two_factor_secret_encrypted="2fa",
                                         export_json_encrypted="pool-codex")
                db.add_all([team, entry]); await db.commit()
                auth = SimpleNamespace(login_entry=AsyncMock(return_value=AccountPoolLoginResult(
                    payload=export_payload(), workspace={"workspace_id": "workspace-a"})))
                result = await TeamBpsExportPreparation(auth).prepare(team.id, db)
                auth.login_entry.assert_awaited_once_with(db, entry.id, "workspace-a")
                self.assertEqual(build_auth_file(result.payload)[1]["type"], "bps")
                self.assertEqual(team.access_token_encrypted, "team-codex")
                self.assertEqual(entry.export_json_encrypted, "pool-codex")
        finally:
            await engine.dispose()
