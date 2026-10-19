"""Exercise scheduled departure through real pool credentials, OAuth and membership persistence."""
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, AccountPoolWorkspace, Team, TeamEmailMapping, TeamReplacementQueue
from app.services.account_pool_authorization import AccountPoolAuthorizationService
from app.services.account_pool_credentials import AccountPoolCredentialService
from app.services.encryption import encryption_service
from app.services.member_auto_kick import MemberAutoKickService
from app.services.member_departure import AccountPoolMemberExit
from app.services.openai_automatic_login import OpenAIAutomaticLoginError
from app.services.team import TeamService
from app.utils.time_utils import get_now

EMAIL = "member@example.com"
SPACE = "workspace-1"
USER = "member-user"
PASSWORD = "test-password"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def login_result(*, user_id=USER):
    access = jwt.encode({
        "email": EMAIL, "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": SPACE, "chatgpt_user_id": user_id, "chatgpt_plan_type": "team",
        },
    }, "test-key-with-at-least-32-characters", algorithm="HS256")
    return {"access_token": access, "refresh_token": "new-refresh",
            "workspace": {"workspace_id": SPACE, "status": "workspace_ok"}}


class MemberDepartureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.team = Team(email="owner@example.com", account_id=SPACE,
                         access_token_encrypted="unused", rotation_mode="auto")
        self.entry = AccountPoolEntry(
            email=EMAIL, workspace_id="personal", workspace_status="personal_account",
            password_encrypted=encryption_service.encrypt_token(PASSWORD),
            two_factor_secret_encrypted=encryption_service.encrypt_token(TOTP_SECRET),
        )
        self.db.add_all([self.team, self.entry])
        await self.db.flush()
        self.mapping = TeamEmailMapping(
            team_id=self.team.id, email=EMAIL, status="joined", member_role="standard-user",
            upstream_user_id=USER, seat_type="premium", joined_at=get_now() - timedelta(hours=3),
            auto_kick_at=get_now() - timedelta(hours=1),
        )
        self.db.add(self.mapping)
        await self.db.commit()
        self._configure_services()

    def _configure_services(self):
        self.remote = SimpleNamespace(
            delete_workspace_user=AsyncMock(return_value={"success": True, "status_code": 204}),
            delete_member=AsyncMock(return_value={"success": True, "status_code": 204}),
            get_members=AsyncMock(return_value={"success": True, "members": [], "total": 0}),
            create_oauth_authorize_url=Mock(return_value={"state": "state", "code_verifier": "verifier"}),
        )
        self.teams = TeamService()
        self.teams.chatgpt_service = self.remote
        self.teams.ensure_access_token = AsyncMock(return_value="owner-token")
        self.teams.sync_team_info = AsyncMock(return_value={"success": True})
        self.teams._reset_error_status = AsyncMock()
        self.login = SimpleNamespace(login=AsyncMock(return_value=login_result()),
                                     verify_credentials=AsyncMock(return_value={
                                         "available_workspaces": [{"id": "personal", "is_personal": True}],
                                     }))
        self.authorization = AccountPoolAuthorizationService(
            AccountPoolCredentialService(encryption_service), self.login, self.remote,
            team_service=self.teams,
        )
        self.exit = AccountPoolMemberExit(authorization=self.authorization, remote=self.remote)

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def remove(self):
        return await self.teams.delete_team_member(
            self.team.id, USER, self.db, email=EMAIL, member_exit=self.exit,
        )

    async def test_due_rotation_member_logs_in_with_pool_password_and_2fa_then_exits(self):
        scanner = MemberAutoKickService()
        invite = AsyncMock()

        async def remove(team_id, user_id, db, *, email):
            return await self.teams.delete_team_member(
                team_id, user_id, db, email=email, member_exit=self.exit,
            )

        stats = await scanner.run_due_members(self.db, remove, invite_replacement=invite)
        self.assertEqual((stats["scanned"], stats["kicked"], stats["failed"]), (1, 1, 0))
        request = self.login.login.await_args.args[0]
        self.assertEqual((request.email, request.password, request.totp_secret, request.account_id),
                         (EMAIL, PASSWORD, TOTP_SECRET, SPACE))
        self.remote.delete_workspace_user.assert_awaited_once()
        self.assertEqual(self.remote.delete_workspace_user.await_args.args[0],
                         self.login.login.return_value["access_token"])
        self.remote.delete_member.assert_not_awaited()
        self.assertEqual(self.mapping.status, "removed")
        self.assertEqual(self.entry.workspace_id, "personal")
        self.assertEqual(self.team.pending_replacements, 0)
        self.assertEqual(list((await self.db.scalars(select(TeamReplacementQueue))).all()), [])
        workspace = (await self.db.scalars(select(AccountPoolWorkspace))).one()
        self.assertEqual(workspace.workspace_id, SPACE)
        self.assertTrue(workspace.export_json_encrypted)
        invite.assert_not_awaited()

    async def test_deactivated_member_login_uses_admin_removal(self):
        self.login.login.side_effect = OpenAIAutomaticLoginError("account_deactivated")
        with self.assertLogs("app.services.member_departure", "WARNING") as logs:
            result = await self.remove()
        self.assertTrue(result["success"])
        self.assertEqual(result["removal_method"], "admin")
        self.assertIn("account_deactivated", " ".join(logs.output))
        self.remote.delete_workspace_user.assert_not_awaited()
        self.remote.delete_member.assert_awaited_once()
        self.assertEqual(self.mapping.status, "removed")

    async def test_member_self_exit_forbidden_uses_admin_removal(self):
        self.remote.delete_workspace_user.return_value = {
            "success": False, "status_code": 403, "error": "self exit forbidden",
        }
        with self.assertLogs("app.services.member_departure", "WARNING"):
            result = await self.remove()
        self.assertEqual(result["removal_method"], "admin")
        self.remote.delete_member.assert_awaited_once_with(
            "owner-token", SPACE, USER, self.db, identifier=self.team.email,
        )

    async def test_missing_pool_2fa_uses_admin_removal(self):
        self.entry.two_factor_secret_encrypted = None
        await self.db.commit()
        with self.assertLogs("app.services.member_departure", "WARNING"):
            result = await self.remove()
        self.assertTrue(result["success"])
        self.login.login.assert_not_awaited()
        self.remote.delete_member.assert_awaited_once()

    async def test_wrong_login_identity_is_never_used_to_exit(self):
        self.login.login.return_value = login_result(user_id="other-user")
        with self.assertLogs("app.services.member_departure", "WARNING"):
            result = await self.remove()
        self.assertEqual(result["removal_method"], "admin")
        self.remote.delete_workspace_user.assert_not_awaited()
        self.remote.delete_member.assert_awaited_once()

    async def test_changed_deadline_or_exemption_cancels_deletion(self):
        for changes in ({"auto_kick_exempt": True}, {"auto_kick_at": get_now() + timedelta(hours=1)},
                        {"status": "removed"}, {"upstream_user_id": "other-user"}):
            with self.subTest(changes=changes):
                self.mapping.auto_kick_exempt = False
                self.mapping.auto_kick_at = get_now() - timedelta(hours=1)
                self.mapping.status = "joined"
                self.mapping.upstream_user_id = USER
                for name, value in changes.items():
                    setattr(self.mapping, name, value)
                await self.db.commit()
                result = await self.remove()
                self.assertFalse(result["success"])
        self.login.login.assert_not_awaited()
        self.remote.delete_workspace_user.assert_not_awaited()
        self.remote.delete_member.assert_not_awaited()

    async def test_admin_failure_and_unconfirmed_snapshot_keep_member_joined(self):
        self.login.login.side_effect = OpenAIAutomaticLoginError("account_deactivated")
        self.remote.delete_member.return_value = {"success": False, "status_code": 403, "error": "forbidden"}
        with self.assertLogs("app.services.member_departure", "WARNING"):
            result = await self.remove()
        self.assertFalse(result["success"])
        self.assertEqual(self.mapping.status, "joined")
        self.login.login.side_effect = None
        self.remote.get_members.return_value = {"success": False, "error": "snapshot failed"}
        with patch("app.services.team.asyncio.sleep", new=AsyncMock()):
            result = await self.remove()
        self.assertFalse(result["success"])
        self.assertEqual(self.mapping.status, "joined")

    async def test_self_login_is_attempted_even_if_owner_credentials_are_unavailable(self):
        self.teams.ensure_access_token.return_value = None
        result = await self.remove()
        self.login.login.assert_awaited_once()
        self.remote.delete_workspace_user.assert_awaited_once()
        self.assertTrue(result["success"])
        self.login.verify_credentials.assert_awaited_once()
        self.assertEqual(self.mapping.status, "removed")
        self.teams.sync_team_info.assert_not_awaited()
        self.teams._reset_error_status.assert_not_awaited()
        self.remote.delete_member.assert_not_awaited()

    async def test_owner_unavailable_never_treats_unknown_or_visible_team_as_departure(self):
        self.teams.ensure_access_token.return_value = None
        for scan in ({}, {"available_workspaces": []},
                     {"available_workspaces": [{"id": SPACE, "is_personal": False}]}):
            with self.subTest(scan=scan):
                self.login.verify_credentials.return_value = scan
                result = await self.remove()
                self.assertFalse(result["success"])
                self.assertEqual(self.mapping.status, "joined")
        self.remote.delete_member.assert_not_awaited()

    async def test_exemption_changed_during_login_cancels_both_delete_methods(self):
        async def login(_request):
            self.mapping.auto_kick_exempt = True
            await self.db.commit()
            return login_result()

        self.login.login.side_effect = login
        result = await self.remove()
        self.assertFalse(result["success"])
        self.remote.delete_workspace_user.assert_not_awaited()
        self.remote.delete_member.assert_not_awaited()
