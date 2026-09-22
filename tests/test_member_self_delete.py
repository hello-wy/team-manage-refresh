import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import MemberAuthorization, Team
from app.services.chatgpt import ChatGPTService
from app.services.encryption import encryption_service
from app.services.team import TeamService
from app.utils.jwt_parser import JWTParser


def member_token(user_id="member-user", email="member@example.com"):
    return jwt.encode(
        {
            "email": email,
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "workspace-1",
                "chatgpt_user_id": user_id,
            },
        },
        "unit-test-key-with-at-least-32-chars",
        algorithm="HS256",
    )


class ChatGPTDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_workspace_user_retries_409_and_sends_required_headers(self):
        service = ChatGPTService()
        service._make_request = AsyncMock(side_effect=[
            {"success": False, "status_code": 409, "error": "conflict"},
            {"success": False, "status_code": 409, "error": "conflict"},
            {"success": True, "status_code": 204, "data": {}},
        ])

        with patch("app.services.chatgpt.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await service.delete_workspace_user(
                "member-token", "workspace-1", "member/user", object(), identifier="member-1"
            )

        self.assertTrue(result["success"])
        self.assertEqual(sleep.await_args_list[0].args, (5,))
        self.assertEqual(sleep.await_args_list[1].args, (10,))
        request = service._make_request.await_args
        headers = request.args[2]
        self.assertEqual(headers["Authorization"], "Bearer member-token")
        self.assertEqual(headers["chatgpt-account-id"], "workspace-1")
        self.assertTrue(headers["oai-device-id"])
        self.assertIn("member%2Fuser", request.args[1])


class TeamMemberDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = self.sessions()
        self.db.add(Team(
            id=1,
            email="owner@example.com",
            access_token_encrypted="owner-encrypted",
            account_id="workspace-1",
            max_members=5,
            current_members=2,
        ))
        await self.db.commit()
        self.member_token = member_token()
        self.remote = SimpleNamespace(
            delete_workspace_user=AsyncMock(return_value={"success": True, "status_code": 204}),
            delete_member=AsyncMock(return_value={"success": True, "status_code": 204}),
            get_members=AsyncMock(return_value={"success": True, "members": [], "total": 0}),
            refresh_access_token_with_refresh_token=AsyncMock(),
        )
        self.service = TeamService.__new__(TeamService)
        self.service.chatgpt_service = self.remote
        self.service.jwt_parser = JWTParser()
        self.service.ensure_access_token = AsyncMock(return_value="owner-token")
        self.service.sync_team_info = AsyncMock()
        self.service._reset_error_status = AsyncMock()
        self.service._release_member_seat = AsyncMock()
        self.service.mark_team_email_mapping_removed = AsyncMock()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def _save_member_authorization(self):
        self.db.add(MemberAuthorization(
            team_id=1,
            account_id="workspace-1",
            email="member@example.com",
            credentials_encrypted=encryption_service.encrypt_token(json.dumps({
                "access_token": self.member_token,
                "refresh_token": "member-refresh",
                "client_id": "member-client",
                "chatgpt_user_id": "member-user",
            })),
        ))
        await self.db.commit()

    async def _save_member_export_json(self):
        payload = {
            "accounts": [{"credentials": {
                "access_token": self.member_token,
                "chatgpt_user_id": "member-user",
            }}],
        }
        self.db.add(MemberAuthorization(
            team_id=1,
            account_id="workspace-1",
            email="member@example.com",
            export_json_encrypted=encryption_service.encrypt_token(json.dumps(payload)),
        ))
        await self.db.commit()

    async def test_member_json_is_used_before_owner_fallback(self):
        await self._save_member_authorization()

        result = await self.service._delete_team_member_locked(
            1, "member-user", self.db, email="member@example.com"
        )

        self.assertTrue(result["success"])
        self.assertIn("自身授权", result["message"])
        self.remote.delete_workspace_user.assert_awaited_once()
        self.remote.delete_member.assert_not_awaited()
        self.remote.get_members.assert_awaited_once_with(
            "owner-token", "workspace-1", self.db, identifier="owner@example.com"
        )

    async def test_saved_export_json_can_drive_member_self_delete(self):
        await self._save_member_export_json()

        result = await self.service._delete_team_member_locked(
            1, "member-user", self.db, email="member@example.com"
        )

        self.assertTrue(result["success"])
        self.remote.delete_workspace_user.assert_awaited_once()
        self.remote.delete_member.assert_not_awaited()

    async def test_member_401_refreshes_member_json_and_retries_self_delete(self):
        await self._save_member_authorization()
        refreshed_token = member_token()
        self.remote.delete_workspace_user.side_effect = [
            {"success": False, "status_code": 401, "error": "expired"},
            {"success": True, "status_code": 204},
        ]
        self.remote.refresh_access_token_with_refresh_token.return_value = {
            "success": True,
            "access_token": refreshed_token,
            "refresh_token": "member-refresh-2",
        }

        result = await self.service._delete_team_member_locked(
            1, "member-user", self.db, email="member@example.com"
        )

        self.assertTrue(result["success"])
        self.assertEqual(self.remote.delete_workspace_user.await_count, 2)
        self.remote.delete_member.assert_not_awaited()
        self.remote.refresh_access_token_with_refresh_token.assert_awaited_once()

    async def test_missing_member_json_uses_owner_delete_and_verifies_snapshot(self):
        result = await self.service._delete_team_member_locked(
            1, "member-user", self.db, email="member@example.com"
        )

        self.assertTrue(result["success"])
        self.remote.delete_workspace_user.assert_not_awaited()
        self.remote.delete_member.assert_awaited_once_with(
            "owner-token", "workspace-1", "member-user", self.db, identifier="owner@example.com"
        )
        self.remote.get_members.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
