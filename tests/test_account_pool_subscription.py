import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.database import Base
from app.models import AccountPoolEntry
from app.routes.admin import account_pool_page
from app.services.account_pool_authorization import AccountPoolAuthorizationService
from app.services.account_pool_listing import build_pool_entry_data
from app.services.account_pool_liveness import AccountPoolLivenessService
from app.services.account_pool_import import AccountPoolImportService
from app.services.account_pool_profile import credential_personal_plan, save_import_account_name
from app.services.account_pool_workspace_sync import persist_workspace_scan
from app.services.encryption import encryption_service
from app.services.openai_subscription import normalize_personal_plan, personal_subscription, query_personal_subscription
from app.services.openai_workspace import inspect_workspace_claims, merge_workspace_scans

EMAIL = "personal@example.com"


def tokens(plan="plus", **auth_fields):
    claims = {"email": EMAIL, "name": "Personal User", "exp": 4_070_908_800,
              "https://api.openai.com/auth": {"chatgpt_account_id": "personal",
                                             "chatgpt_plan_type": plan, **auth_fields}}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"access_token": f"header.{encoded}.signature", "refresh_token": "refresh",
            "workspace": {"workspace_id": "personal", "is_personal": True}}


class SubscriptionParsingTests(unittest.TestCase):
    def test_explicit_names_and_tiers_are_normalized(self):
        for value, expected in [("free", "Free"), ("chatgptplusplan", "Plus"),
                                ("pro_100", "Pro100"), ("chatgpt_pro_200", "Pro200"),
                                ("Pro 500", "Pro500"), ("pro", "Pro200"),
                                ("prolite", "Pro100"), ("promax", "Pro500"), ("go", "Go")]:
            with self.subTest(value=value):
                self.assertEqual(normalize_personal_plan(value), expected)
        for value in (None, 200, "personal", "team", "business", "pro_unlimited", "<Plus>"):
            self.assertEqual(normalize_personal_plan(value), "")

    def test_explicit_subscription_tier_refines_generic_pro(self):
        self.assertEqual(personal_subscription({"plan_type": "pro", "entitlement": {
            "subscription_plan": "chatgptpro100plan"}}), "Pro100")
        self.assertEqual(credential_personal_plan(tokens("pro", subscription_plan="pro_500")), "Pro500")
        self.assertEqual(credential_personal_plan(tokens("pro")), "Pro200")
        self.assertEqual(credential_personal_plan(tokens("team")), "")
        self.assertEqual(personal_subscription({"plan_type": "pro", "entitlement": {
            "plan_type": "free"}}), "Pro200")

    def test_queried_plan_is_authoritative_over_older_claims(self):
        for current in ("Free", "Plus", "Pro100", "Pro200", "Pro500"):
            with self.subTest(current=current):
                self.assertEqual(personal_subscription({"personal_plan": current,
                                                        "plan_type": "promax"}), current)
        self.assertEqual(personal_subscription({"plan_type": "pro_200",
                                               "entitlement": {"plan_type": "pro_500"}}), "Pro200")
        credentials = {**tokens("promax"), "personal_plan": "Free"}
        self.assertEqual(credential_personal_plan(credentials), "Free")
        self.assertEqual(credential_personal_plan({**tokens("team"), "personal_plan": "Pro500"}), "")

    def test_workspace_discovery_and_merging_preserve_subscription(self):
        scan = inspect_workspace_claims({"workspaces": [{
            "id": "personal", "plan_type": "pro", "subscription_plan": "pro_200",
        }]})
        self.assertTrue(scan["is_personal"])
        merged = merge_workspace_scans([scan, {"available_workspaces": [{
            "id": "personal", "name": "Personal", "is_personal": True,
        }]}])
        self.assertEqual(merged["available_workspaces"][0]["personal_plan"], "Pro200")


class SubscriptionQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_uses_authenticated_session_and_workspace_header(self):
        session = SimpleNamespace(get=AsyncMock(return_value=Mock(
            status_code=200, json=Mock(return_value={"plan_type": "prolite"}))))
        self.assertEqual(await query_personal_subscription(session, "access", "personal-id"), "Pro100")
        session.get.assert_awaited_once_with(
            "https://chatgpt.com/backend-api/wham/usage",
            headers={"Authorization": "Bearer access", "Accept": "application/json",
                     "Chatgpt-Account-Id": "personal-id"}, timeout=10,
        )

    async def test_query_failures_and_malformed_responses_stay_unknown(self):
        responses = [Mock(status_code=401), Mock(status_code=200, json=Mock(side_effect=ValueError)),
                     Mock(status_code=200, json=Mock(return_value=[])),
                     Mock(status_code=200, json=Mock(return_value={"plan_type": "team"}))]
        for response in responses:
            with self.subTest(response=response):
                session = SimpleNamespace(get=AsyncMock(return_value=response))
                self.assertEqual(await query_personal_subscription(session, "access"), "")
        session = SimpleNamespace(get=AsyncMock(side_effect=TimeoutError))
        self.assertEqual(await query_personal_subscription(session, "access"), "")
        session.get.reset_mock()
        self.assertEqual(await query_personal_subscription(session, ""), "")
        session.get.assert_not_awaited()


class SubscriptionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.entry = AccountPoolEntry(id=1, email=EMAIL)
        self.db.add(self.entry)
        await self.db.commit()
        self.auth = AccountPoolAuthorizationService(Mock(), Mock(), Mock(), team_service=Mock())

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def row(self):
        await self.db.flush()
        await self.db.refresh(self.entry)
        return (await build_pool_entry_data(self.db, [self.entry]))[0]

    async def test_import_saves_subscription_without_storing_tokens(self):
        await save_import_account_name(self.db, 1, tokens("pro_500"))
        await self.db.commit()
        self.assertEqual((await self.row())["personal_plan"], "Pro500")
        self.assertIsNone(self.entry.export_json_encrypted)
        self.assertNotIn("access_token", self.entry.workspace_state_json)

    async def test_import_retains_queried_plan_instead_of_older_token_plan(self):
        result = {**tokens("plus"), "subscription": {"personal_plan": "Pro200"}}
        login = self.auth._result(EMAIL, result, "personal")
        authorization = SimpleNamespace(login_entry=AsyncMock(return_value=login))
        service = AccountPoolImportService(authorization, Mock(), save_import_account_name)
        self.assertIsNone(await service._populate_entry(self.db, 1, email=EMAIL))
        self.assertEqual((await self.row())["personal_plan"], "Pro200")
        self.assertIsNone(self.entry.export_json_encrypted)
        self.assertNotIn("access_token", self.entry.workspace_state_json)
        authorization.login_entry.assert_awaited_once()

    async def test_queried_plan_replaces_stale_workspace_plan(self):
        result = tokens("pro")
        result["workspace"]["personal_plan"] = "Pro500"
        result["subscription"] = {"personal_plan": "Pro200"}
        login = self.auth._result(EMAIL, result, "personal")
        await self.auth.save_result(self.db, 1, login)
        self.assertEqual((await self.row())["personal_plan"], "Pro200")

    async def test_login_updates_the_selected_subscription_and_keeps_workspace_choices(self):
        self.entry.workspace_state_json = json.dumps({"available_workspaces": [{
            "id": "personal", "name": "Personal", "is_personal": True, "personal_plan": "Free",
        }]})
        login = self.auth._result(EMAIL, tokens("pro", subscription_plan="pro_100"), "personal")
        await self.auth.save_result(self.db, 1, login)
        self.assertEqual((await self.row())["personal_plan"], "Pro100")
        state = json.loads(self.entry.workspace_state_json)
        self.assertEqual(state["available_workspaces"][0]["personal_plan"], "Pro100")

    async def test_workspace_refresh_retains_known_plan_and_accepts_new_plan(self):
        self.entry.workspace_state_json = json.dumps({"personal_plan": "Plus"})
        await self.db.commit()
        for discovered_plan, expected in [(None, "Plus"), ("Pro200", "Pro200")]:
            item = {"id": "personal", "name": "Personal", "is_personal": True}
            if discovered_plan:
                item["personal_plan"] = discovered_plan
            await persist_workspace_scan(self.db, self.entry, discovered=[item], credential_version=(None, None))
            self.assertEqual((await self.row())["personal_plan"], expected)

    async def test_legacy_export_is_read_and_missing_or_invalid_data_stays_unknown(self):
        self.entry.export_json_encrypted = encryption_service.encrypt_token(json.dumps(tokens("plus")))
        self.assertEqual((await self.row())["personal_plan"], "Plus")
        self.entry.export_json_encrypted = encryption_service.encrypt_token("{}")
        self.assertEqual((await self.row())["personal_plan"], "")
        self.entry.export_json_encrypted = None
        self.assertEqual((await self.row())["personal_plan"], "")

    async def test_unknown_personal_login_is_not_assumed_free(self):
        login = self.auth._result(EMAIL, tokens(""), "personal")
        await self.auth.save_result(self.db, 1, login)
        self.assertEqual(login.workspace["plan_type"], "personal")
        self.assertEqual((await self.row())["personal_plan"], "")

    async def test_team_scan_keeps_personal_plan_when_personal_workspace_is_hidden(self):
        self.entry.workspace_state_json = json.dumps({"available_workspaces": [{
            "id": "personal", "is_personal": True, "personal_plan": "Pro200",
        }]})
        await self.db.commit()
        await persist_workspace_scan(self.db, self.entry, discovered=[{
            "id": "team", "name": "Team", "is_personal": False,
        }], credential_version=(None, None))
        await self.db.refresh(self.entry)
        self.assertEqual(json.loads(self.entry.workspace_state_json)["personal_plan"], "Pro200")
        await persist_workspace_scan(self.db, self.entry, discovered=[], credential_version=(None, None))
        self.assertEqual((await self.row())["personal_plan"], "Pro200")

    async def test_liveness_updates_plan_without_changing_membership_or_saving_tokens(self):
        self.entry.workspace_state_json = json.dumps({"available_workspaces": [{
            "id": "personal", "is_personal": True, "personal_plan": "Plus",
        }]})
        await self.db.commit()
        authorization = SimpleNamespace(scan_entry=AsyncMock(return_value={"available_workspaces": [{
            "id": "personal", "is_personal": True, "personal_plan": "Pro100",
        }]}))
        credentials = SimpleNamespace(get_credentials=AsyncMock(return_value={"password": "saved"}))
        status = await AccountPoolLivenessService(credentials, authorization).check_entry(self.db, 1)
        self.assertEqual(status, "alive")
        self.assertEqual((await self.row())["personal_plan"], "Pro100")
        self.assertIsNone(self.entry.workspace_id)
        self.assertIsNone(self.entry.export_json_encrypted)

    async def test_page_displays_each_personal_plan_and_unknown_badge(self):
        for index, plan in enumerate(("Free", "Plus", "Pro100", "Pro200", "Pro500", ""), start=2):
            self.db.add(AccountPoolEntry(id=index, email=f"plan{index}@example.com",
                                        workspace_status="personal_account",
                                        workspace_state_json=json.dumps({"personal_plan": plan})))
        await self.db.commit()
        request = Request({"type": "http", "method": "GET", "path": "/admin/account-pool",
                           "headers": [(b"x-requested-with", b"XMLHttpRequest")],
                           "query_string": b"", "server": ("localhost", 8008)})
        page = await account_pool_page(request, db=self.db, current_user={"username": "admin"})
        html = page.body.decode()
        for plan in ("Free", "Plus", "Pro100", "Pro200", "Pro500", "订阅待确认"):
            self.assertIn(f">{plan}</span>", html)
        self.assertIn('<dialog id="accountPoolImportDialog"', html)
        self.assertNotIn("accountPoolImportPanel", html)
