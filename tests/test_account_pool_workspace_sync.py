"""Workspace refresh must confirm memberships and update the pool's joined-Team view."""
import base64
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from unittest.mock import patch

import jwt

from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, Team, TeamEmailMapping
from app.services.account_pool import account_pool_service
from app.services.account_pool_authorization import AccountPoolAuthorizationError, AccountPoolAuthorizationService
from app.services.account_pool_credentials import account_pool_credential_service
from app.services.team import TeamService

MEMBERS = (
    {"id": "member-a", "email": "member-a@example.com", "name": "成员甲",
     "role": "standard-user", "seat_type": "default", "created_time": "2026-10-15T13:13:00"},
    {"id": "member-b", "email": "member-b@example.com", "name": "成员乙",
     "role": "standard-user", "seat_type": "prolite", "created_time": "2026-10-15T13:15:00"},
)
PERSONAL_SCAN = {"available_workspaces": [{"id": "personal", "name": "Personal", "is_personal": True}]}
TOKEN_EXPIRES_AT = 4_070_908_800


def login_result(email, workspace_id):
    claims = {
        "email": email,
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": workspace_id,
            "chatgpt_user_id": "member-user",
            "chatgpt_plan_type": "team",
        },
    }
    token = jwt.encode(claims, "unit-test-key-with-at-least-32-chars", algorithm="HS256")
    return {
        "success": True,
        "access_token": token,
        "refresh_token": "refresh-token",
        "id_token": token,
        "workspace": {"status": "workspace_ok", "workspace_id": workspace_id},
    }


class AccountPoolWorkspaceSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = self.sessions()
        self.team = Team(id=1, email="owner@example.com", account_id="team-space",
                         team_name="已加入的 Team", access_token_encrypted="unused", max_members=5)
        self.db.add(self.team)
        await self.db.commit()
        self.remote = SimpleNamespace(
            get_members=AsyncMock(return_value={"success": True, "members": list(MEMBERS), "total": 2}),
            get_invites=AsyncMock(return_value={"success": True, "items": []}),
            get_seat_capacity=AsyncMock(return_value={"success": True, "data": {"seat_capacity": [
                {"type": "default", "paid": 3, "available": 2},
                {"type": "prolite", "paid": 2, "available": 1},
            ]}}),
        )
        self.teams = TeamService()
        self.teams.chatgpt_service = self.remote
        self.teams.ensure_access_token = AsyncMock(return_value="owner-token")
        self.login = SimpleNamespace(verify_credentials=AsyncMock(return_value=PERSONAL_SCAN), login=AsyncMock())
        self.authorization = AccountPoolAuthorizationService(
            account_pool_credential_service, self.login,
            Mock(create_oauth_authorize_url=Mock(return_value={"state": "test-state"})),
            team_service=self.teams,
        )

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def add_member(self, email, *, linked=True):
        await account_pool_credential_service.add_accounts(
            self.db, content=f"{email}----password----JBSWY3DPEHPK3PXP",
        )
        entry = (await self.db.execute(select(AccountPoolEntry).where(AccountPoolEntry.email == email))).scalar_one()
        entry.workspace_id = "personal"
        entry.workspace_status = "personal_account"
        entry.workspace_state_json = json.dumps(PERSONAL_SCAN)
        if linked:
            self.db.add(TeamEmailMapping(team_id=1, email=email, status="invited"))
        await self.db.commit()
        return entry

    async def test_refresh_recognizes_both_joined_members_despite_personal_only_scan(self):
        entries = [await self.add_member(member["email"]) for member in MEMBERS]
        for entry in entries:
            state = await self.authorization.refresh_team_list(self.db, entry.id)
            self.assertEqual(state["available_workspaces"], [{
                "id": "team-space", "name": "已加入的 Team", "is_personal": False,
            }])
            self.assertEqual(entry.workspace_id, "team-space")
            self.assertEqual(entry.workspace_status, "workspace_ok")
        listing = await account_pool_service.list_entries(self.db, status_filter="joined", search="member-")
        self.assertEqual({row["email"] for row in listing["entries"]}, {member["email"] for member in MEMBERS})
        self.assertEqual({row["seat_type"] for row in listing["entries"]}, {"standard", "premium"})
        self.assertTrue(all(row["display_team_name"] == "已加入的 Team" for row in listing["entries"]))
        personal = await account_pool_service.list_entries(self.db, team_filter="personal", search="member-")
        self.assertEqual(personal["total"], 0)
        mappings = (await self.db.execute(select(TeamEmailMapping))).scalars().all()
        self.assertTrue(all(mapping.joined_at for mapping in mappings))
        by_seat = {mapping.seat_type: mapping for mapping in mappings}
        self.assertIsNotNone(by_seat["standard"].auto_kick_at)
        self.assertIsNotNone(by_seat["premium"].auto_kick_at)
        self.login.login.assert_not_awaited()

    async def test_discovered_managed_team_synchronizes_previously_unknown_member(self):
        entry = await self.add_member(MEMBERS[0]["email"], linked=False)
        self.login.verify_credentials.return_value = {"available_workspaces": [
            {"id": "team-space", "name": "OAuth Team", "is_personal": False},
        ]}
        await self.authorization.refresh_team_list(self.db, entry.id)
        row = (await account_pool_service.rows_by_ids(self.db, [entry.id]))[0]
        self.assertEqual(row["status"], "joined")
        self.assertEqual(row["seat_type"], "standard")
        self.assertEqual(row["team_id"], self.team.id)

    async def test_hidden_emails_are_resolved_by_authorized_user_id_and_remain_synced(self):
        entries = [await self.add_member(member["email"]) for member in MEMBERS]
        self.remote.get_members.return_value = {
            "success": True, "members": [{**member, "email": None} for member in MEMBERS], "total": 2,
        }
        self.login.verify_credentials.return_value = {"available_workspaces": [
            {"id": "team-space", "name": "Team", "is_personal": False},
            *PERSONAL_SCAN["available_workspaces"],
        ]}

        async def login(request):
            member = next(member for member in MEMBERS if member["email"] == request.email)
            claims = {"exp": TOKEN_EXPIRES_AT, "email": request.email,
                      "https://api.openai.com/auth": {
                          "chatgpt_account_id": "team-space", "chatgpt_user_id": member["id"],
                          "chatgpt_plan_type": "team",
                      }}
            encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            return {"success": True, "access_token": f"header.{encoded}.signature",
                    "refresh_token": "test-refresh-token"}

        self.login.login.side_effect = login
        for entry in entries:
            await self.authorization.refresh_team_list(self.db, entry.id)
            row = (await account_pool_service.rows_by_ids(self.db, [entry.id]))[0]
            self.assertEqual(row["status"], "joined")
            self.assertEqual(entry.workspace_id, "team-space")
        snapshot = await self.teams.get_team_members(self.team.id, self.db)
        self.assertEqual({member["email"] for member in snapshot["members"]},
                         {member["email"] for member in MEMBERS})
        self.assertTrue(all(member["name"]
                            and not member["profile_sync_error"] for member in snapshot["members"]))
        by_seat = {member["seat_type"]: member for member in snapshot["members"]}
        self.assertIsNotNone(by_seat["default"]["auto_kick_at"])
        self.assertTrue(by_seat["default"]["auto_kick_enabled"])
        self.assertIsNotNone(by_seat["prolite"]["auto_kick_at"])
        self.login.login.reset_mock()
        await self.authorization.refresh_team_list(self.db, entries[0].id)
        self.login.login.assert_not_awaited()

    async def test_refresh_endpoint_renders_the_actual_member_seat(self):
        from app.routes import admin

        entry = await self.add_member(MEMBERS[0]["email"])
        with patch.object(admin, "account_pool_authorization_service", self.authorization):
            response = await admin.scan_account_pool_workspace(entry.id, self.db, {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["workspace"]["available_workspaces"][0]["id"], "team-space")
        cell = await self.seat_cell(entry.email)
        self.assertIn("标准", cell)
        self.assertNotIn("个人账户", cell)

    async def test_external_discovered_team_does_not_render_as_personal(self):
        entry = await self.add_member(MEMBERS[0]["email"], linked=False)
        self.login.verify_credentials.return_value = {"available_workspaces": [
            {"id": "external-team", "name": "External Team", "is_personal": False},
        ]}
        await self.authorization.refresh_team_list(self.db, entry.id)
        cell = await self.seat_cell(entry.email)
        self.assertIn("席位待确认", cell)
        self.assertNotIn("个人账户", cell)
        self.remote.get_members.assert_not_awaited()

    async def seat_cell(self, email):
        from app.routes.admin import account_pool_page

        request = Request({"type": "http", "method": "GET", "path": "/admin/account-pool",
                           "headers": [], "query_string": b"", "server": ("test", 80)})
        response = await account_pool_page(request, search=email, db=self.db, current_user={"username": "admin"})
        html = response.body.decode()
        row = html.partition(f'data-email="{email}"')[2].split("</tr>", 1)[0]
        return row.partition('<td data-label="Team / 席位">')[2].split("</td>", 1)[0]

    async def test_invited_member_is_not_falsely_marked_joined(self):
        entry = await self.add_member("pending@example.com")
        self.remote.get_invites.return_value = {"success": True, "items": [{
            "email_address": entry.email, "state": "pending",
        }]}
        self.login.verify_credentials.return_value = {"available_workspaces": [
            {"id": "team-space", "name": "OAuth Team", "is_personal": False},
        ]}
        state = await self.authorization.refresh_team_list(self.db, entry.id)
        self.assertEqual(state["available_workspaces"], [])
        row = (await account_pool_service.rows_by_ids(self.db, [entry.id]))[0]
        self.assertEqual(row["status"], "invited")

    async def test_member_read_failure_does_not_save_personal_account_as_success(self):
        entry = await self.add_member(MEMBERS[0]["email"])
        entry.workspace_id = "team-space"
        entry.workspace_status = "workspace_ok"
        await self.db.commit()
        self.remote.get_members.return_value = {"success": False, "error": "member endpoint unavailable"}
        with self.assertRaisesRegex(AccountPoolAuthorizationError, "成员核对失败"):
            await self.authorization.refresh_team_list(self.db, entry.id)
        self.assertEqual(entry.workspace_id, "team-space")
        self.assertEqual(entry.workspace_status, "workspace_ok")

    async def test_revoked_owner_token_uses_member_oauth_to_confirm_discovered_team(self):
        entry = await self.add_member(MEMBERS[0]["email"])
        self.remote.get_members.return_value = {
            "success": False,
            "error_code": "token_revoked",
            "error": '{"error":{"code":"token_revoked"}}',
        }
        self.login.verify_credentials.return_value = {"available_workspaces": [
            {"id": "team-space", "name": "OAuth Team", "is_personal": False},
        ]}
        self.login.login.side_effect = lambda request: login_result(
            request.email, workspace_id=request.account_id,
        )

        state = await self.authorization.refresh_team_list(self.db, entry.id)

        self.assertEqual(state["available_workspaces"], [{
            "id": "team-space", "name": "已加入的 Team", "is_personal": False,
        }])
        self.assertEqual(entry.workspace_id, "team-space")
        self.login.login.assert_awaited_once()

    async def test_stale_removed_mapping_is_not_reused_without_fresh_discovery(self):
        entry = await self.add_member(MEMBERS[0]["email"])
        mapping = (await self.db.execute(select(TeamEmailMapping))).scalar_one()
        mapping.status = "removed"
        await self.db.commit()
        await self.authorization.refresh_team_list(self.db, entry.id)
        self.remote.get_members.assert_not_awaited()
        self.assertEqual(entry.workspace_status, "personal_account")

    async def test_credential_change_during_scan_rejects_stale_workspace_result(self):
        entry = await self.add_member(MEMBERS[0]["email"], linked=False)

        async def change_credentials(request):
            await account_pool_credential_service.update_credentials(self.db, entry.id, password="new-password")
            return PERSONAL_SCAN

        self.login.verify_credentials.side_effect = change_credentials
        with self.assertRaisesRegex(AccountPoolAuthorizationError, "凭据已变化"):
            await self.authorization.refresh_team_list(self.db, entry.id)
