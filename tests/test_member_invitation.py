"""Regression coverage for manual invitations with live typed seat capacity."""
import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Team, TeamEmailMapping
from app.services.team import TeamService
from app.utils.time_utils import get_now


class MemberInvitationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add(Team(id=1, email="owner@example.com", account_id="account",
                        access_token_encrypted="test", current_members=6, max_members=6, status="full"))
            await db.commit()
        self.service = TeamService()
        self.service.ensure_access_token = AsyncMock(return_value="token")
        self.service.sync_team_info = AsyncMock(return_value={"success": True, "member_emails": []})
        self.service.get_team_seat_balance = AsyncMock(return_value={"success": True, "balance": {
            "premium": {"known": True, "remaining": 3},
        }})
        self.service.chatgpt_service = SimpleNamespace(send_invite=AsyncMock(return_value={
            "success": True, "data": {"account_invites": [{"email_address": "target@example.com"}]},
        }))
        self.service._background_verify_admin_invite = AsyncMock()

    async def asyncTearDown(self):
        await asyncio.gather(*self.service._background_tasks)
        await self.engine.dispose()

    async def _recent_invite(self):
        async with self.sessions() as db:
            db.add(TeamEmailMapping(team_id=1, email="target@example.com", status="removed",
                                    last_invited_at=get_now() - timedelta(hours=1)))
            await db.commit()

    async def test_single_invite_uses_premium_balance_when_legacy_limit_is_full(self):
        async with self.sessions() as db:
            result = await self.service.add_team_member(1, "target@example.com", db, "premium")
            team = await db.get(Team, 1)
            self.assertEqual(team.current_members, 7)
        self.assertTrue(result["success"], result)
        self.service.chatgpt_service.send_invite.assert_awaited_once()
        self.assertEqual(self.service.chatgpt_service.send_invite.await_args.kwargs["seat_type"], "premium")

    async def test_batch_uses_premium_balance_when_legacy_limit_is_full(self):
        async with self.sessions() as db:
            result = await self.service.add_team_members(1, ["target@example.com"], db, "premium")
        self.assertEqual(result["summary"]["invited"], 1, result)
        self.assertEqual(result["summary"]["no_seat"], 0)

    async def test_explicit_manual_reinvite_is_allowed_and_recorded(self):
        await self._recent_invite()
        async with self.sessions() as db:
            result = await self.service.add_team_members(
                1, ["target@example.com"], db, "premium", allow_reinvite=True,
            )
            mapping = (await db.execute(select(TeamEmailMapping))).scalar_one()
            self.assertEqual(mapping.status, "invited")
            self.assertTrue(mapping.is_admin_invited)
        self.assertEqual(result["summary"]["invited"], 1, result)

    async def test_automatic_invite_retains_cooldown_and_exposes_reason(self):
        await self._recent_invite()
        async with self.sessions() as db:
            result = await self.service.add_team_members(1, ["target@example.com"], db, "premium")
        self.assertFalse(result["success"])
        self.assertIn("7 天", result["message"])
        self.assertEqual(result["results"][0]["error_code"], "team_reinvite_cooldown")
        self.service.chatgpt_service.send_invite.assert_not_awaited()

    async def test_manual_reinvite_does_not_bypass_official_seat_balance(self):
        await self._recent_invite()
        self.service.get_team_seat_balance.return_value["balance"]["premium"]["remaining"] = 0
        async with self.sessions() as db:
            result = await self.service.add_team_members(
                1, ["target@example.com"], db, "premium", allow_reinvite=True,
            )
        self.assertEqual(result["error_code"], "seat_limit_exceeded")
        self.service.chatgpt_service.send_invite.assert_not_awaited()

    async def test_upstream_rejection_keeps_detailed_error_in_batch_response(self):
        self.service.chatgpt_service.send_invite.return_value = {
            "success": False, "status_code": 403, "error": "Invitation is restricted", "error_code": "invite_restricted",
        }
        async with self.sessions() as db:
            result = await self.service.add_team_members(1, ["target@example.com"], db, "premium")
        self.assertFalse(result["success"])
        self.assertIn("Invitation is restricted", result["message"])
        self.assertEqual(result["results"][0]["error_code"], "invite_restricted")
