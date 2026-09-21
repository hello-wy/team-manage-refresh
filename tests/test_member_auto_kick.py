import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Team, TeamEmailMapping, TeamReplacementQueue
from app.services.member_auto_kick import MemberAutoKickService
from app.services.team import TeamService
from app.utils.time_utils import get_now


class MemberAutoKickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.service = MemberAutoKickService()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_team(self, session, team_id=1):
        team = Team(
            id=team_id,
            email=f"owner-{team_id}@example.com",
            access_token_encrypted="token",
            account_id=f"account-{team_id}",
            current_members=2,
            max_members=5,
            status="active",
        )
        session.add(team)
        await session.commit()
        return team

    async def test_reconcile_generates_deadline_and_excludes_owner(self):
        joined_at = get_now() - timedelta(minutes=10)
        async with self.session_factory() as session:
            await self._seed_team(session)
            team_service = TeamService.__new__(TeamService)
            members = {
                "member@example.com": {
                    "id": "user-member",
                    "role": "standard-user",
                    "created_time": joined_at.isoformat(),
                },
                "owner@example.com": {
                    "id": "user-owner",
                    "role": "account-owner",
                    "created_time": joined_at.isoformat(),
                },
            }
            await team_service._reconcile_team_email_mappings(
                1,
                set(members),
                set(),
                session,
                joined_members=members,
            )
            await session.commit()

            mappings = (await session.execute(select(TeamEmailMapping))).scalars().all()
            by_email = {mapping.email: mapping for mapping in mappings}
            self.assertEqual(
                by_email["member@example.com"].auto_kick_at,
                joined_at + timedelta(hours=2),
            )
            self.assertIsNone(by_email["owner@example.com"].auto_kick_at)

    async def test_update_hours_recalculates_existing_deadlines(self):
        joined_at = get_now() - timedelta(hours=1)
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            session.add(TeamEmailMapping(
                team_id=team.id,
                email="member@example.com",
                status="joined",
                source="sync",
                upstream_user_id="user-member",
                member_role="standard-user",
                joined_at=joined_at,
                auto_kick_at=joined_at + timedelta(hours=2),
            ))
            await session.commit()

            result = await self.service.update_team_hours(team.id, 6, session)
            mapping = (await session.execute(select(TeamEmailMapping))).scalar_one()

            self.assertTrue(result["success"])
            self.assertEqual(team.member_auto_kick_hours, 6)
            self.assertEqual(mapping.auto_kick_at, joined_at + timedelta(hours=6))

    async def test_member_exemption_clears_deadline_and_restores_it(self):
        joined_at = get_now() - timedelta(hours=1)
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            session.add(TeamEmailMapping(
                team_id=team.id,
                email="member@example.com",
                status="joined",
                source="sync",
                upstream_user_id="user-member",
                member_role="standard-user",
                joined_at=joined_at,
                auto_kick_at=joined_at + timedelta(hours=2),
            ))
            await session.commit()

            exempted = await self.service.update_member_exemption(
                team.id, "user-member", True, session
            )
            mapping = (await session.execute(select(TeamEmailMapping))).scalar_one()
            self.assertTrue(exempted["success"])
            self.assertTrue(mapping.auto_kick_exempt)
            self.assertIsNone(mapping.auto_kick_at)

            restored = await self.service.update_member_exemption(
                team.id, "user-member", False, session
            )
            mapping = (await session.execute(select(TeamEmailMapping))).scalar_one()
            self.assertTrue(restored["success"])
            self.assertFalse(mapping.auto_kick_exempt)
            self.assertEqual(mapping.auto_kick_at, joined_at + timedelta(hours=2))

    async def test_due_scan_skips_exempt_member(self):
        now = get_now()
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            session.add(TeamEmailMapping(
                team_id=team.id,
                email="protected@example.com",
                status="joined",
                source="sync",
                upstream_user_id="user-protected",
                member_role="standard-user",
                joined_at=now - timedelta(hours=3),
                auto_kick_at=now - timedelta(hours=1),
                auto_kick_exempt=True,
            ))
            await session.commit()

            stats = await self.service.run_due_members(
                session,
                AsyncMock(return_value={"success": True}),
                invite_replacement=AsyncMock(
                    return_value={"success": True, "status": "invited"}
                ),
            )

            self.assertEqual(stats["scanned"], 0)
            self.assertEqual(stats["kicked"], 0)

    async def test_due_scan_kicks_only_due_non_owner(self):
        now = get_now()
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            session.add_all([
                TeamEmailMapping(
                    team_id=team.id,
                    email="due@example.com",
                    status="joined",
                    source="sync",
                    upstream_user_id="user-due",
                    member_role="standard-user",
                    joined_at=now - timedelta(hours=3),
                    auto_kick_at=now - timedelta(hours=1),
                ),
                TeamEmailMapping(
                    team_id=team.id,
                    email="later@example.com",
                    status="joined",
                    source="sync",
                    upstream_user_id="user-later",
                    member_role="standard-user",
                    joined_at=now,
                    auto_kick_at=now + timedelta(hours=2),
                ),
                TeamEmailMapping(
                    team_id=team.id,
                    email="owner@example.com",
                    status="joined",
                    source="sync",
                    upstream_user_id="user-owner",
                    member_role="account-owner",
                    joined_at=now - timedelta(days=1),
                    auto_kick_at=now - timedelta(hours=1),
                ),
            ])
            await session.commit()
            delete_member = AsyncMock(return_value={"success": True})
            invite_replacement = AsyncMock(
                return_value={"success": True, "status": "invited"}
            )

            stats = await self.service.run_due_members(
                session,
                delete_member,
                invite_replacement=invite_replacement,
            )

            self.assertEqual(stats, {
                "success": True,
                "scanned": 1,
                "kicked": 1,
                "failed": 0,
                "replacement_invited": 1,
                "replacement_unavailable": 0,
                "replacement_failed": 0,
                "replacement_pending": 0,
            })
            delete_member.assert_awaited_once_with(
                team.id,
                "user-due",
                session,
                email="due@example.com",
            )
            invite_replacement.assert_awaited_once_with(team.id, session, "standard")

    async def test_due_scan_reports_delete_failure(self):
        now = get_now()
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            session.add(TeamEmailMapping(
                team_id=team.id,
                email="due@example.com",
                status="joined",
                source="sync",
                upstream_user_id="user-due",
                member_role="standard-user",
                joined_at=now - timedelta(hours=3),
                auto_kick_at=now - timedelta(hours=1),
            ))
            await session.commit()

            stats = await self.service.run_due_members(
                session,
                AsyncMock(return_value={"success": False, "error": "upstream failed"}),
                invite_replacement=AsyncMock(
                    return_value={"success": True, "status": "invited"}
                ),
            )

            self.assertEqual(stats, {
                "success": False,
                "scanned": 1,
                "kicked": 0,
                "failed": 1,
                "replacement_invited": 0,
                "replacement_unavailable": 0,
                "replacement_failed": 0,
                "replacement_pending": 0,
            })

    async def test_replacement_keeps_kicked_member_seat_type(self):
        now = get_now()
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            session.add(TeamEmailMapping(
                team_id=team.id,
                email="premium@example.com",
                status="joined",
                source="sync",
                upstream_user_id="user-premium",
                member_role="standard-user",
                seat_type="premium",
                joined_at=now - timedelta(hours=3),
                auto_kick_at=now - timedelta(hours=1),
            ))
            await session.commit()
            invite_replacement = AsyncMock(
                return_value={"success": True, "status": "invited"}
            )

            await self.service.run_due_members(
                session,
                AsyncMock(return_value={"success": True}),
                invite_replacement=invite_replacement,
            )

            invite_replacement.assert_awaited_once_with(team.id, session, "premium")
            queue = (await session.execute(select(TeamReplacementQueue))).scalars().all()
            self.assertEqual(queue, [])

    async def test_due_scan_reports_replacement_failure(self):
        now = get_now()
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            session.add(TeamEmailMapping(
                team_id=team.id,
                email="due@example.com",
                status="joined",
                upstream_user_id="user-due",
                member_role="standard-user",
                joined_at=now - timedelta(hours=3),
                auto_kick_at=now - timedelta(hours=1),
            ))
            await session.commit()

            with self.assertLogs("app.services.member_auto_kick", "WARNING") as logs:
                stats = await self.service.run_due_members(
                    session,
                    AsyncMock(return_value={"success": True}),
                    invite_replacement=AsyncMock(return_value={
                        "success": False,
                        "status": "failed",
                        "email": "candidate@example.com",
                        "error_code": "seat_operation_pending",
                        "status_code": 409,
                        "error": "已有未确认邀请",
                    }),
                )

            self.assertFalse(stats["success"])
            self.assertEqual(stats["kicked"], 1)
            self.assertEqual(stats["replacement_failed"], 1)
            self.assertEqual(stats["replacement_pending"], 1)
            self.assertIn("email=candidate@example.com", logs.output[0])
            self.assertIn("error_code=seat_operation_pending", logs.output[0])
            self.assertIn("status_code=409", logs.output[0])

    async def test_pending_replacement_retries_on_next_scan(self):
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            team.pending_replacements = 1
            session.add(TeamReplacementQueue(team_id=team.id, seat_type="premium"))
            await session.commit()

            unavailable = await self.service.run_due_members(
                session,
                AsyncMock(),
                invite_replacement=AsyncMock(
                    return_value={"success": True, "status": "no_candidate"}
                ),
            )
            self.assertEqual(unavailable["replacement_pending"], 1)
            self.assertEqual(unavailable["replacement_unavailable"], 1)

            invited = await self.service.run_due_members(
                session,
                AsyncMock(),
                invite_replacement=AsyncMock(
                    return_value={"success": True, "status": "invited"}
                ),
            )
            self.assertEqual(invited["replacement_invited"], 1)
            self.assertEqual(invited["replacement_pending"], 0)

    async def test_pending_count_is_repaired_from_replacement_queue(self):
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            team.pending_replacements = 2
            session.add(TeamReplacementQueue(team_id=team.id, seat_type="premium"))
            await session.commit()

            with self.assertLogs("app.services.member_auto_kick", "ERROR") as logs:
                stats = await self.service.run_due_members(
                    session,
                    AsyncMock(),
                    invite_replacement=AsyncMock(
                        return_value={"success": True, "status": "no_candidate"}
                    ),
                )

            self.assertEqual(team.pending_replacements, 1)
            self.assertEqual(stats["replacement_pending"], 1)
            self.assertEqual(stats["replacement_unavailable"], 1)
            self.assertIn("自动补位队列不一致", logs.output[0])

    async def test_missing_queue_is_not_invited_as_standard(self):
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            team.pending_replacements = 1
            await session.commit()
            invite_replacement = AsyncMock()

            stats = await self.service.run_due_members(
                session,
                AsyncMock(),
                invite_replacement=invite_replacement,
            )

            self.assertEqual(team.pending_replacements, 0)
            self.assertEqual(stats["replacement_pending"], 0)
            invite_replacement.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
