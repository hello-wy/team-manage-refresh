import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.database import get_db
from app.dependencies.auth import require_admin
from app.main import app
from app.routes import admin
from app.services.chatgpt import ChatGPTService
from app.services.team import TeamService
from app.utils.seats import calculate_seat_balance, normalize_seat_type, summarize_member_seats


class MemberSeatSummaryTests(unittest.TestCase):
    def test_separates_joined_invited_unknown_and_pending(self):
        summary = summarize_member_seats([
            {"status": "joined", "seat_type": "default", "pending_seat_type": "premium"},
            {"status": "joined", "seat_type": "premium"},
            {"status": "joined", "seat_type": "usage_based"},
            {"status": "invited", "seat_type": "standard"},
            {"status": "invited", "seat_type": None},
        ], invites_complete=False)
        self.assertEqual(summary["joined"], {"standard": 1, "premium": 1, "unknown": 1, "total": 3})
        self.assertEqual(summary["invited"], {"standard": 1, "premium": 0, "unknown": 1, "total": 2})
        self.assertFalse(summary["invites_complete"])


class MemberSeatServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = TeamService()
        self.remote = SimpleNamespace(get_members=AsyncMock(), update_member_seat_type=AsyncMock())
        self.service.chatgpt_service = self.remote
        self.service.ensure_access_token = AsyncMock(return_value="test-token")
        self.service.get_team_seat_balance = AsyncMock(return_value={"success": True, "balance": {
            "standard": {"known": True, "remaining": 10}, "premium": {"known": True, "remaining": 10},
        }})
        self.service._reserve_member_seat = AsyncMock(return_value=None)
        self.service._release_member_seat = AsyncMock()
        self.team = SimpleNamespace(id=1, account_id="account-1", email="owner@example.com")
        self.db = AsyncMock()
        self.db.get.return_value = self.team
        self.remote.update_member_seat_type.return_value = {"success": True, "data": {"success": True}}

    def member_response(self, seat="default", pending=None, user_id="member-1"):
        return {"success": True, "members": [{"id": user_id, "seat_type": seat, "pending_seat_type": pending}]}

    async def change(self, seat="premium", expected="default"):
        return await self.service.update_member_seat_type(1, "member-1", seat, expected, self.db)

    async def test_change_requires_remote_readback(self):
        self.remote.get_members.side_effect = [self.member_response(), self.member_response("premium")]
        result = await self.change()
        self.assertEqual(result["status"], "applied")
        self.remote.update_member_seat_type.assert_awaited_once_with(
            "test-token", "account-1", "member-1", "premium", self.db, identifier="owner@example.com",
        )

    async def test_downgrade_pending_is_not_counted_as_applied(self):
        self.remote.get_members.side_effect = [self.member_response("premium"), self.member_response("premium", "default")]
        result = await self.change("default", "premium")
        self.assertEqual(result["status"], "pending")

    async def test_stale_selection_does_not_overwrite_remote_change(self):
        self.remote.get_members.return_value = self.member_response("premium")
        result = await self.change()
        self.assertEqual(result["error_code"], "seat_changed")
        self.remote.update_member_seat_type.assert_not_awaited()

    async def test_pending_change_blocks_new_change(self):
        self.remote.get_members.return_value = self.member_response("default", "premium")
        result = await self.change()
        self.assertEqual(result["error_code"], "seat_change_pending")
        self.remote.update_member_seat_type.assert_not_awaited()

    async def test_foreign_member_cannot_be_updated(self):
        self.remote.get_members.return_value = self.member_response(user_id="another-member")
        result = await self.change()
        self.assertEqual(result["error_code"], "member_not_found")
        self.remote.update_member_seat_type.assert_not_awaited()

    async def test_unchanged_selection_skips_write(self):
        self.remote.get_members.return_value = self.member_response()
        self.assertEqual((await self.change("default"))["status"], "unchanged")
        self.remote.update_member_seat_type.assert_not_awaited()

    async def test_rejected_change_returns_error(self):
        self.remote.get_members.return_value = self.member_response()
        self.remote.update_member_seat_type.return_value = {"success": False, "error": "No available premium seats"}
        result = await self.change()
        self.assertFalse(result["success"])
        self.assertIn("No available", result["error"])

    async def test_timeout_with_successful_readback_is_applied(self):
        self.remote.get_members.side_effect = [self.member_response(), self.member_response("premium")]
        self.remote.update_member_seat_type.return_value = {"success": False, "error": "timeout"}
        self.assertEqual((await self.change())["status"], "applied")
        self.remote.update_member_seat_type.assert_awaited_once()

    async def test_accepted_but_unconfirmed_is_not_applied(self):
        self.remote.get_members.side_effect = [self.member_response(), {"success": False, "error": "timeout"}]
        self.assertEqual((await self.change())["status"], "unconfirmed")

    async def test_bulk_invite_preserves_selected_seat_type(self):
        self.team.max_members = 5
        self.team.current_members = 1
        self.service.sync_team_info = AsyncMock(return_value={"success": True, "member_emails": []})
        self.service.add_team_member = AsyncMock(return_value={"success": True, "status": "invited"})
        result = await self.service.add_team_members(1, ["one@example.com", "two@example.com"], self.db, seat_type="premium")
        self.assertEqual(result["summary"]["invited"], 2)
        for call in self.service.add_team_member.await_args_list:
            self.assertEqual(call.kwargs["seat_type"], "premium")


class MemberSeatRemoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_invite_counts_include_all_pages(self):
        remote = ChatGPTService()
        responses = [
            {"success": True, "data": {"items": [{"seat_type": "default"}], "total": 2}},
            {"success": True, "data": {"items": [{"seat_type": "premium"}], "total": 2}},
        ]
        with patch.object(remote, "_make_request", new=AsyncMock(side_effect=responses)) as request:
            result = await remote.get_invites("token", "account", None)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["items"][1]["seat_type"], "premium")
        self.assertIn("offset=1", request.await_args.args[1])

    async def test_incomplete_invite_pages_are_not_reported_as_complete(self):
        remote = ChatGPTService()
        with patch.object(remote, "_make_request", new=AsyncMock(return_value={"success": True, "data": {"items": [], "total": 2}})):
            result = await remote.get_invites("token", "account", None)
        self.assertFalse(result["success"])

    async def test_invite_payload_preserves_legacy_default(self):
        remote = ChatGPTService()
        with patch.object(remote, "_make_request", new=AsyncMock(return_value={"success": True})) as request:
            await remote.send_invite("token", "account", "member@example.com", None)
            self.assertNotIn("seat_type", request.await_args.args[3])
            await remote.send_invite("token", "account", "member@example.com", None, seat_type="premium")
            self.assertEqual(request.await_args.args[3]["seat_type"], "prolite")
            await remote.send_invite("token", "account", "member@example.com", None, seat_type="default")
            self.assertEqual(request.await_args.args[3]["seat_type"], "default")

    async def test_patch_does_not_retry_network_failure(self):
        remote = ChatGPTService()
        session = SimpleNamespace(patch=AsyncMock(side_effect=TimeoutError("test timeout")))
        with patch.object(remote, "_get_session", new=AsyncMock(return_value=session)):
            result = await remote.update_member_seat_type("token", "account", "member", "premium", None)
        self.assertFalse(result["success"])
        session.patch.assert_awaited_once()
        self.assertEqual(session.patch.await_args.args[0], "https://chatgpt.com/backend-api/accounts/account/users/member")
        self.assertEqual(session.patch.await_args.kwargs["json"], {"seat_type": "prolite"})


class MemberSeatRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.previous_overrides = app.dependency_overrides.copy()

    def tearDown(self):
        app.dependency_overrides = self.previous_overrides
        self.client.close()

    def test_requires_admin(self):
        response = self.client.post("/admin/teams/1/members/member/seat-type", json={"seat_type": "premium", "expected_seat_type": "default"})
        self.assertIn(response.status_code, (401, 403))

    def test_rejects_unknown_seat_type(self):
        app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        app.dependency_overrides[get_db] = lambda: None
        response = self.client.post("/admin/teams/1/members/member/seat-type", json={"seat_type": "invalid", "expected_seat_type": "default"})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/admin/teams/1/members/add", json={"emails": ["test@example.com"], "seat_type": "invalid"})
        self.assertEqual(response.status_code, 422)

    def test_seat_change_route_forwards_selected_and_expected_types(self):
        app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        app.dependency_overrides[get_db] = lambda: None
        result = {"success": True, "status": "applied", "message": "席位类型已更新"}
        with patch.object(admin.team_service, "update_member_seat_type", new=AsyncMock(return_value=result)) as update:
            response = self.client.post("/admin/teams/1/members/member/seat-type", json={"seat_type": "premium", "expected_seat_type": "default"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "applied")
        update.assert_awaited_once_with(1, "member", "premium", "default", None)

    def test_invite_route_forwards_seat_type(self):
        app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        app.dependency_overrides[get_db] = lambda: None
        with patch.object(admin.team_service, "add_team_members", new=AsyncMock(return_value={"success": True, "processed": True})) as invite:
            response = self.client.post("/admin/teams/1/members/add", json={"emails": ["test@example.com"], "seat_type": "premium"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(invite.await_args.kwargs["seat_type"], "premium")

    def test_account_pool_options_route_returns_service_entries(self):
        app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        app.dependency_overrides[get_db] = lambda: None
        entries = [{"id": 7, "email": "pool@example.com", "seat_type": "premium"}]
        with patch.object(admin.account_pool_service, "list_invite_options", new=AsyncMock(return_value=entries)) as list_options:
            response = self.client.get("/admin/account-pool/options?team_id=3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entries"], entries)
        list_options.assert_awaited_once_with(3, None)
