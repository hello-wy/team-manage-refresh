"""Timed departure: joined time plus Team hours, with per-member participation."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, RotationAction, Team, TeamEmailMapping
from app.services.account_pool_rotation import attach_rotation_status
from app.services.member_auto_kick import MemberAutoKickService
from app.services.member_departure import AccountPoolMemberExit
from app.services.team import TeamService
from app.utils.time_utils import get_now

EMAIL = "member@example.com"
USER_ID = "member-user"
TEAM_ID = 1
JOINED_AT = "2026-10-15T13:13:00"


class MemberAutoKickParticipationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.db.add(Team(id=TEAM_ID, email="owner@example.com", account_id="space",
                         access_token_encrypted="unused"))
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

    async def participate(self, enabled):
        result = await self.auto_kick.update_member_participation(TEAM_ID, USER_ID, enabled, self.db)
        self.assertTrue(result["success"], result.get("error"))
        return result

    async def set_hours(self, hours):
        return await self.auto_kick.update_team_hours(TEAM_ID, hours, self.db)

    def deadline(self, hours):
        return datetime.fromisoformat(JOINED_AT) + timedelta(hours=hours)

    async def test_every_seat_participates_by_default_from_join_time(self):
        for seat in ("default", "standard", "premium", "prolite"):
            with self.subTest(seat=seat):
                member = await self.fetch(seat_type=seat)
                self.assertTrue(member["auto_kick_enabled"])
                self.assertEqual(member["auto_kick_at"], self.deadline(2).isoformat())

    async def test_owner_never_participates(self):
        member = await self.fetch(role="account-owner")
        self.assertIsNone(member["auto_kick_at"])
        result = await self.auto_kick.update_member_participation(TEAM_ID, USER_ID, True, self.db)
        self.assertFalse(result["success"])

    async def test_team_hours_apply_only_to_participating_members(self):
        await self.fetch()
        result = await self.set_hours(48)
        self.assertTrue(result["success"])
        self.assertEqual((await self.mapping()).auto_kick_at, self.deadline(48))
        await self.participate(False)
        await self.set_hours(72)
        self.assertIsNone((await self.mapping()).auto_kick_at)
        member = await self.fetch()
        self.assertFalse(member["auto_kick_enabled"])
        self.assertIsNone(member["auto_kick_at"])
        await self.participate(True)
        self.assertEqual((await self.mapping()).auto_kick_at, self.deadline(72))

    async def test_invalid_team_hours_are_rejected(self):
        await self.fetch()
        for hours in (0, 8761):
            with self.subTest(hours=hours):
                self.assertFalse((await self.set_hours(hours))["success"])
        self.assertEqual((await self.db.get(Team, TEAM_ID)).member_auto_kick_hours, 2)

    async def test_toggle_keeps_rotation_exemption_in_sync(self):
        await self.fetch()
        await self.participate(False)
        mapping = await self.mapping()
        self.assertEqual((mapping.auto_kick_enabled, mapping.auto_kick_exempt), (False, True))
        await self.participate(True)
        self.assertEqual((mapping.auto_kick_enabled, mapping.auto_kick_exempt), (True, False))

    async def test_rejoining_member_participates_again(self):
        await self.fetch()
        await self.participate(False)
        mapping = await self.mapping()
        mapping.status = "removed"
        await self.db.commit()
        member = await self.fetch()
        self.assertTrue(member["auto_kick_enabled"])
        self.assertEqual(member["auto_kick_at"], self.deadline(2).isoformat())

    async def test_only_participating_due_members_are_scanned(self):
        await self.fetch()
        mapping = await self.mapping()
        self.assertEqual([m.email for m in await self.auto_kick._due_members(self.db)], [EMAIL])
        self.assertTrue(AccountPoolMemberExit._still_due(mapping, USER_ID))
        mapping.auto_kick_enabled = False
        await self.db.commit()
        delete = AsyncMock()
        self.assertEqual((await self.auto_kick.run_due_members(self.db, delete))["scanned"], 0)
        delete.assert_not_awaited()
        self.assertFalse(AccountPoolMemberExit._still_due(mapping, USER_ID))

    async def test_failures_back_off_and_are_reported(self):
        await self.fetch()
        mapping = await self.mapping()
        delete = AsyncMock(return_value={"success": False, "error": "管理员凭据失效"})
        expected = [timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=2)]
        for attempt, delay in enumerate(expected, 1):
            with self.subTest(attempt=attempt):
                before = get_now()
                stats = await self.auto_kick.run_due_members(self.db, delete)
                self.assertEqual(stats["failed"], 1)
                self.assertEqual(mapping.auto_kick_failures, attempt)
                self.assertEqual(mapping.auto_kick_last_error, "管理员凭据失效")
                self.assertGreaterEqual(mapping.auto_kick_retry_at, before + delay)
                self.assertLess(mapping.auto_kick_retry_at, get_now() + delay)
                self.assertEqual((await self.auto_kick.run_due_members(self.db, delete))["scanned"], 0)
                mapping.auto_kick_retry_at = get_now() - timedelta(seconds=1)
                await self.db.commit()
        self.assertEqual(delete.await_count, len(expected))
        member = await self.fetch()
        self.assertEqual(member["auto_kick_failures"], len(expected))
        self.assertEqual(member["auto_kick_last_error"], "管理员凭据失效")

    async def test_changing_deadline_resets_failures(self):
        await self.fetch()
        mapping = await self.mapping()
        await self.auto_kick.run_due_members(self.db, AsyncMock(return_value={"success": False, "error": "x"}))
        self.assertEqual(mapping.auto_kick_failures, 1)
        await self.set_hours(24 * 30)
        self.assertEqual((mapping.auto_kick_failures, mapping.auto_kick_last_error, mapping.auto_kick_retry_at),
                         (0, None, None))

    async def test_failure_is_not_recorded_when_member_stops_participating_during_kick(self):
        await self.fetch()
        mapping = await self.mapping()

        async def disabled_then_failed(*args, **kwargs):
            await self.participate(False)
            return {"success": False, "error": "成员的下线计划或身份已变化，取消本次下线"}

        await self.auto_kick.run_due_members(self.db, disabled_then_failed)
        self.assertEqual((mapping.auto_kick_failures, mapping.auto_kick_last_error), (0, None))

    async def test_open_rotation_action_postpones_kick(self):
        await self.fetch()
        self.db.add(RotationAction(team_id=TEAM_ID, email=EMAIL, action_type="upgrade",
                                   status="executing", idempotency_key="upgrade-1"))
        await self.db.commit()
        self.assertEqual(await self.auto_kick._due_members(self.db), [])

    async def test_rotation_status_shows_schedule_failures_and_non_participation(self):
        team = await self.db.get(Team, TEAM_ID)
        team.rotation_mode = "dry_run"
        await self.fetch()
        await self.set_hours(24 * 365)
        entry = AccountPoolEntry(email=EMAIL)
        self.db.add(entry)
        await self.db.commit()
        row = {"id": entry.id, "email": EMAIL}
        await attach_rotation_status(self.db, [row])
        self.assertEqual(row["rotation"]["label"], "定时下线")
        self.assertIn("额度轮转照常进行", row["rotation"]["reason"])
        self.assertFalse(row["rotation"]["reason"].startswith("仅预览"))
        mapping = await self.mapping()
        mapping.auto_kick_last_error = "管理员凭据失效"
        mapping.auto_kick_retry_at = get_now() + timedelta(minutes=5)
        await self.db.commit()
        await attach_rotation_status(self.db, [row])
        self.assertEqual((row["rotation"]["label"], row["rotation"]["kind"]), ("下线失败", "blocked"))
        self.assertIn("管理员凭据失效", row["rotation"]["reason"])
        team.rotation_mode = "off"
        await self.participate(False)
        await attach_rotation_status(self.db, [row])
        self.assertEqual(row["rotation"]["label"], "保留当前 Team")
        self.assertIn("未参与自动下线", row["rotation"]["reason"])


if __name__ == "__main__":
    unittest.main()
