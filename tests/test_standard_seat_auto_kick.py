"""Standard seats opt out of timed departure until explicitly enabled."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, Team, TeamEmailMapping
from app.services.account_pool_rotation import attach_rotation_status
from app.services.member_auto_kick import MemberAutoKickService
from app.services.member_departure import AccountPoolMemberExit
from app.services.team import TeamService
from app.utils.time_utils import get_now

EMAIL = "member@example.com"
USER_ID = "member-user"
TEAM_ID = 1
HOURS = 2
JOINED_AT = "2026-10-15T13:13:00"


class StandardSeatAutoKickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.db.add(Team(id=TEAM_ID, email="owner@example.com", account_id="space",
                         access_token_encrypted="unused", member_auto_kick_hours=HOURS))
        await self.db.commit()
        self.remote = SimpleNamespace(
            get_members=AsyncMock(),
            get_invites=AsyncMock(return_value={"success": True, "items": []}),
            get_seat_capacity=AsyncMock(return_value={"success": True, "data": {
                "seat_capacity": [{"type": "default", "paid": 5, "available": 4},
                                  {"type": "prolite", "paid": 5, "available": 4}],
            }}),
        )
        self.teams = TeamService()
        self.teams.chatgpt_service = self.remote
        self.teams.ensure_access_token = AsyncMock(return_value="test-token")
        self.auto_kick = MemberAutoKickService()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def fetch(self, **fields):
        self.remote.get_members.return_value = {"success": True, "total": 1, "members": [{
            "id": USER_ID, "email": EMAIL, "role": "standard-user", "name": "成员甲",
            "seat_type": "default", "created_time": JOINED_AT, **fields,
        }]}
        result = await self.teams.get_team_members(TEAM_ID, self.db)
        self.assertTrue(result["success"], result.get("error"))
        return result["members"][0]

    async def mapping(self):
        return (await self.db.scalars(select(TeamEmailMapping))).one()

    async def set_exempt(self, exempt):
        result = await self.auto_kick.update_member_exemption(
            TEAM_ID, USER_ID, exempt, self.db,
        )
        self.assertTrue(result["success"], result.get("error"))
        return result

    async def test_standard_aliases_are_exempt_while_premium_still_gets_deadline(self):
        for seat in ("default", "standard", "premium", "prolite"):
            with self.subTest(seat=seat):
                member = await self.fetch(seat_type=seat)
                standard = seat in ("default", "standard")
                self.assertEqual(member["auto_kick_exempt"], standard)
                self.assertEqual(member["auto_kick_at"] is None, standard)
        mapping = await self.mapping()
        self.assertFalse(mapping.auto_kick_exempt)
        self.assertFalse(mapping.standard_auto_kick_enabled)

    async def test_manual_enable_persists_across_refresh_and_hours_changes(self):
        await self.fetch()
        result = await self.set_exempt(False)
        expected = datetime.fromisoformat(JOINED_AT) + timedelta(hours=HOURS)
        self.assertFalse(result["exempt"])
        self.assertEqual(result["auto_kick_at"], expected.isoformat())
        self.assertFalse((await self.fetch())["auto_kick_exempt"])
        await self.auto_kick.update_team_hours(TEAM_ID, HOURS * 2, self.db)
        mapping = await self.mapping()
        self.assertTrue(mapping.standard_auto_kick_enabled)
        self.assertEqual(mapping.auto_kick_at, expected + timedelta(hours=HOURS))

    async def test_manual_disable_survives_refresh_and_hours_changes(self):
        await self.fetch()
        await self.set_exempt(False)
        result = await self.set_exempt(True)
        self.assertTrue(result["exempt"])
        self.assertIsNone(result["auto_kick_at"])
        await self.auto_kick.update_team_hours(TEAM_ID, HOURS * 2, self.db)
        self.assertTrue((await self.fetch())["auto_kick_exempt"])
        self.assertFalse((await self.mapping()).standard_auto_kick_enabled)

    async def test_saving_team_hours_does_not_enable_default_standard_members(self):
        await self.fetch()
        await self.auto_kick.update_team_hours(TEAM_ID, HOURS * 2, self.db)
        self.assertIsNone((await self.mapping()).auto_kick_at)
        self.assertTrue((await self.fetch())["auto_kick_exempt"])

    async def test_missing_seat_uses_confirmed_identity_cache_without_enabling_exit(self):
        await self.fetch()
        member = await self.fetch(seat_type=None, email=None)
        self.assertEqual(member["seat_type"], "standard")
        self.assertIn("席位类型", member["profile_cached_fields"])
        self.assertTrue(member["auto_kick_exempt"])
        self.assertIsNone((await self.mapping()).auto_kick_at)

    async def test_seat_switch_recalculates_policy_without_changing_manual_exemption(self):
        await self.fetch(seat_type="prolite")
        member = await self.fetch()
        self.assertTrue(member["auto_kick_exempt"])
        self.assertIsNone(member["auto_kick_at"])
        mapping = await self.mapping()
        self.assertFalse(mapping.auto_kick_exempt)
        await self.set_exempt(True)
        self.assertTrue((await self.fetch(seat_type="prolite"))["auto_kick_exempt"])

    async def test_stale_standard_deadline_is_not_scanned_or_used_for_self_exit(self):
        await self.fetch()
        mapping = await self.mapping()
        mapping.auto_kick_at = get_now() - timedelta(hours=HOURS)
        await self.db.commit()
        delete = AsyncMock()
        stats = await self.auto_kick.run_due_members(
            self.db, delete, invite_replacement=AsyncMock(),
        )
        self.assertEqual(stats["scanned"], 0)
        delete.assert_not_awaited()
        self.assertFalse(AccountPoolMemberExit._still_due(mapping, USER_ID))

    async def test_manually_enabled_standard_member_is_scanned_when_due(self):
        await self.fetch()
        await self.set_exempt(False)
        members = await self.auto_kick._due_members(self.db)
        self.assertEqual([member.email for member in members], [EMAIL])

    async def test_stale_standard_deadline_does_not_predict_a_replacement(self):
        await self.fetch()
        mapping = await self.mapping()
        mapping.auto_kick_at = get_now() + timedelta(hours=HOURS)
        member = AccountPoolEntry(email=EMAIL)
        candidate = AccountPoolEntry(email="candidate@example.com")
        self.db.add_all([member, candidate])
        await self.db.commit()
        views = await attach_rotation_status(self.db, [
            {"id": entry.id, "email": entry.email} for entry in (member, candidate)
        ])
        self.assertEqual(views[0]["rotation"]["label"], "保留当前 Team")
        self.assertNotIn("replacement_at", views[1]["rotation"])

    async def test_owner_cannot_be_opted_in(self):
        member = await self.fetch(role="account-owner")
        result = await self.auto_kick.update_member_exemption(
            TEAM_ID, USER_ID, False, self.db,
        )
        self.assertTrue(member["auto_kick_exempt"])
        self.assertFalse(result["success"])
        self.assertIsNone((await self.mapping()).auto_kick_at)
