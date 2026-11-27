import json
import re
import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.database import Base
from app.models import AccountPoolEntry, Team, TeamEmailMapping
from app.routes.admin import account_pool_page
from app.services.account_pool_listing import build_pool_entry_data


class AccountPoolSeatStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.team = Team(id=1, email="owner@example.com", account_id="managed-space",
                         team_name="Managed Team", access_token_encrypted="unused")
        self.entry = AccountPoolEntry(email="member@example.com")
        self.db.add_all([self.team, self.entry])
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    def workspace(self, workspace_id):
        self.entry.workspace_id = workspace_id
        self.entry.workspace_status = "workspace_ok"
        self.entry.workspace_state_json = json.dumps({
            "personal_plan": "Free",
            "available_workspaces": [{"id": workspace_id, "name": "Team", "is_personal": False}],
        })

    async def row_and_html(self):
        await self.db.commit()
        row = (await build_pool_entry_data(self.db, [self.entry]))[0]
        request = Request({"type": "http", "method": "GET", "path": "/admin/account-pool",
                           "headers": [(b"x-requested-with", b"XMLHttpRequest")],
                           "query_string": b"", "server": ("localhost", 8008)})
        page = await account_pool_page(request, db=self.db, current_user={"username": "admin"})
        html = re.search(rf'<tr data-account-pool-row="{self.entry.id}".*?</tr>',
                         page.body.decode(), re.S).group()
        return row, html

    async def test_external_workspace_explains_missing_seat_instead_of_waiting_forever(self):
        self.workspace("external-space")
        saved_state = self.entry.workspace_state_json
        row, html = await self.row_and_html()
        self.assertEqual(row["unconfirmed_subscription"]["label"], "外部 Team")
        self.assertIn("该 Team 未接入本系统管理", html)
        self.assertIn('tabindex="0" data-pool-tooltip=', html)
        self.assertNotIn("席位待确认", html)
        self.assertNotIn(">Free</span>", html)
        self.assertIsNone(row["seat_type"])
        self.assertEqual(self.entry.workspace_state_json, saved_state)

    async def test_joined_member_with_missing_seat_remains_pending(self):
        self.db.add(TeamEmailMapping(team_id=1, email=self.entry.email, status="joined"))
        row, html = await self.row_and_html()
        self.assertEqual(row["unconfirmed_subscription"]["label"], "席位待确认")
        self.assertIn("请刷新 Team 成员列表", html)
        self.assertIsNone(row["seat_type"])

    async def test_confirmed_seat_takes_priority_over_saved_external_workspace(self):
        self.workspace("external-space")
        mapping = TeamEmailMapping(team_id=1, email=self.entry.email, status="joined")
        self.db.add(mapping)
        for seat, label in (("standard", "标准"), ("premium", "高级")):
            with self.subTest(seat=seat):
                mapping.seat_type = seat
                row, html = await self.row_and_html()
                self.assertEqual(row["seat_type"], seat)
                self.assertIsNone(row["unconfirmed_subscription"])
                self.assertIn(f">{label}</span>", html)
                self.assertNotIn("外部 Team", html)

    async def test_historical_managed_workspace_is_not_current_membership(self):
        self.workspace("managed-space")
        mapping = TeamEmailMapping(team_id=1, email=self.entry.email, status="removed")
        self.db.add(mapping)
        row, html = await self.row_and_html()
        self.assertEqual(row["status"], "unassigned")
        self.assertEqual(row["unconfirmed_subscription"]["label"], "归属待核对")
        self.assertIn("历史工作区不代表当前仍在组", html)
        self.assertNotIn("席位待确认", html)
        self.assertNotIn("外部 Team", html)
        self.assertEqual(mapping.status, "removed")

    async def test_invite_is_not_treated_as_confirmed_team_seat_or_personal_account(self):
        self.db.add(TeamEmailMapping(team_id=1, email=self.entry.email, status="invited"))
        row, html = await self.row_and_html()
        self.assertEqual(row["unconfirmed_subscription"]["label"], "待接受邀请")
        self.assertNotIn("席位待确认", html)
        self.assertNotIn("account-pool-personal-label", html)
        self.assertIsNone(row["seat_type"])

    async def test_owner_without_member_seat_is_not_assumed_standard(self):
        self.entry.email = self.team.email
        row, html = await self.row_and_html()
        self.assertTrue(row["is_owner"])
        self.assertEqual(row["unconfirmed_subscription"]["label"], "席位待确认")
        self.assertIsNone(row["seat_type"])
        self.assertNotIn(">标准</span>", html)

    async def test_each_team_keeps_its_own_seat_badge_for_multiple_memberships(self):
        self.db.add(Team(id=2, email="second-owner@example.com", account_id="second-space",
                         team_name="Second Team", access_token_encrypted="unused"))
        self.db.add_all([TeamEmailMapping(team_id=1, email=self.entry.email, status="joined", seat_type="standard"),
                         TeamEmailMapping(team_id=2, email=self.entry.email, status="joined", seat_type="premium")])
        row, html = await self.row_and_html()
        self.assertEqual(row["status"], "conflict")
        self.assertIsNone(row["seat_type"])
        self.assertEqual({item["id"]: item["seat_type"] for item in row["team_options"]}, {1: "standard", 2: "premium"})
        cell = html.partition('<td data-label="归属 / 订阅">')[2].split('</td>')[0]
        self.assertRegex(cell, r'(?s)data-plan="standard".*?Managed Team.*?data-plan="premium".*?Second Team')

    async def test_team_picker_returns_per_type_snapshot_not_only_legacy_counts(self):
        from app.routes.admin import account_pool_team_options
        from app.models import RotationSeatSnapshot
        from app.utils.time_utils import get_now
        self.db.add(RotationSeatSnapshot(team_id=1, standard_paid=10, standard_remaining=2,
                                        premium_paid=3, premium_remaining=0, observed_at=get_now()))
        await self.db.commit()
        data = await account_pool_team_options(db=self.db, current_user={"username": "admin"})
        item = data["teams"][0]
        self.assertFalse(item["seat_balance_needs_refresh"])
        self.assertEqual(item["seat_snapshot"]["standard_remaining"], 2)
        self.assertEqual(item["seat_snapshot"]["premium_remaining"], 0)
