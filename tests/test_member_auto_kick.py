import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, Team, TeamEmailMapping, TeamReplacementQueue
from app.services.account_pool import account_pool_service
from app.services.chatgpt import ChatGPTService
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
                    "role": "standard-user", "seat_type": "premium",
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
            self.assertEqual(by_email["member@example.com"].auto_kick_at, joined_at + timedelta(hours=2))
            self.assertIsNone(by_email["owner@example.com"].auto_kick_at)

    async def test_team_rotation_round_limit_is_saved_and_validated(self):
        async with self.session_factory() as session:
            team = await self._seed_team(session)

            result = await self.service.update_team_hours(1, 3, session, rounds=3)

            self.assertTrue(result["success"])
            self.assertEqual(team.rotation_rounds, 3)
            invalid = await self.service.update_team_hours(1, 3, session, rounds=1001)
            self.assertFalse(invalid["success"])
            self.assertEqual(team.rotation_rounds, 3)

    async def test_replacement_uses_sync_time_instead_of_historical_created_time(self):
        historical = get_now() - timedelta(days=20)
        async with self.session_factory() as session:
            await self._seed_team(session)
            session.add(TeamEmailMapping(
                team_id=1, email="replacement@example.com", status="invited",
                source="auto_replacement", join_source="rotation",
                replacement_export_pending=True, rotation_lane="premium-seat-a", rotation_round=2,
            ))
            await session.commit()

            before_sync = get_now()
            await TeamService()._reconcile_team_email_mappings(
                1,
                {"replacement@example.com"},
                set(),
                session,
                joined_members={"replacement@example.com": {
                    "id": "replacement-user", "role": "standard-user", "seat_type": "premium", "seat_type": "premium",
                    "created_time": historical.isoformat(),
                }},
            )
            await session.commit()

            mapping = (await session.execute(select(TeamEmailMapping))).scalar_one()
            self.assertGreaterEqual(mapping.joined_at, before_sync)
            self.assertGreater(mapping.auto_kick_at, get_now())
            self.assertEqual((mapping.rotation_lane, mapping.rotation_round), ("premium-seat-a", 2))

    async def test_invited_member_starts_countdown_only_when_join_is_confirmed(self):
        invited_at = get_now() - timedelta(days=1)
        async with self.session_factory() as session:
            await self._seed_team(session)
            mapping = TeamEmailMapping(
                team_id=1, email="invitee@example.com", status="invited",
                seat_type=None, last_invited_at=invited_at,
            )
            session.add(mapping)
            await session.commit()
            self.assertIsNone(mapping.auto_kick_at)

            confirmed_at = get_now()
            await TeamService()._reconcile_team_email_mappings(
                1, {"invitee@example.com"}, set(), session,
                joined_members={"invitee@example.com": {
                    "id": "invitee-user", "role": "standard-user", "seat_type": "premium",
                    "created_time": invited_at.isoformat(),
                }},
            )
            await session.commit()

            self.assertGreaterEqual(mapping.joined_at, confirmed_at)
            self.assertGreaterEqual(mapping.auto_kick_at, confirmed_at + timedelta(hours=2))

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
                    member_role="standard-user", seat_type="premium",
                    joined_at=now - timedelta(hours=3),
                    auto_kick_at=now - timedelta(hours=1),
                ),
                TeamEmailMapping(
                    team_id=team.id,
                    email="later@example.com",
                    status="joined",
                    source="sync",
                    upstream_user_id="user-later",
                    member_role="standard-user", seat_type="premium",
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

            stats = await self.service.run_due_members(session, delete_member)

            self.assertEqual(stats, {
                "success": True,
                "scanned": 1,
                "kicked": 1,
                "failed": 0,
            })
            delete_member.assert_awaited_once_with(
                team.id,
                "user-due",
                session,
                email="due@example.com",
            )
            queued = (await session.execute(select(TeamReplacementQueue))).scalars().all()
            self.assertEqual(queued, [])
            self.assertEqual(team.pending_replacements, 0)

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
                member_role="standard-user", seat_type="premium",
                joined_at=now - timedelta(hours=3),
                auto_kick_at=now - timedelta(hours=1),
            ))
            await session.commit()

            stats = await self.service.run_due_members(
                session,
                AsyncMock(return_value={"success": False, "error": "upstream failed"})
            )

            self.assertEqual(stats, {
                "success": False,
                "scanned": 1,
                "kicked": 0,
                "failed": 1,
            })

    async def test_due_premium_member_queues_next_round_and_preserves_seat_lane(self):
        now = get_now()
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            team.rotation_mode = "auto"
            team.rotation_rounds = 3
            mapping = TeamEmailMapping(
                team_id=team.id, email="round-two@example.com", status="joined",
                upstream_user_id="user-round-two", member_role="standard-user",
                seat_type="premium", rotation_lane="premium-seat-a", rotation_round=2,
                joined_at=now - timedelta(hours=3), auto_kick_at=now - timedelta(minutes=1),
            )
            session.add(mapping)
            await session.commit()

            async def delete(*args, **kwargs):
                mapping.status = "removed"
                await session.commit()
                return {"success": True}

            replacement = AsyncMock(return_value={
                "success": True, "status": "invited", "email": "round-three@example.com",
            })
            stats = await self.service.run_due_members(session, delete, replacement)

            self.assertEqual(stats["kicked"], 1)
            self.assertEqual(stats["replacement_invited"], 1)
            replacement.assert_awaited_once_with(
                team.id, "premium", "premium-seat-a", 3, frozenset(), session,
            )
            self.assertEqual(list((await session.scalars(select(TeamReplacementQueue))).all()), [])

    async def test_third_round_ends_without_inviting_a_fourth_account(self):
        now = get_now()
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            team.rotation_mode = "auto"
            team.rotation_rounds = 3
            session.add(TeamEmailMapping(
                team_id=team.id, email="round-three@example.com", status="joined",
                upstream_user_id="user-round-three", member_role="standard-user",
                seat_type="premium", rotation_lane="premium-seat-a", rotation_round=3,
                joined_at=now - timedelta(hours=3), auto_kick_at=now - timedelta(minutes=1),
            ))
            await session.commit()

            async def delete(*args, **kwargs):
                mapping = (await session.scalars(select(TeamEmailMapping))).one()
                mapping.status = "removed"
                await session.commit()
                return {"success": True}

            replacement = AsyncMock()
            stats = await self.service.run_due_members(
                session, delete, replacement,
            )

            self.assertEqual(stats["kicked"], 1)
            self.assertEqual(stats["rotation_finished"], 1)
            replacement.assert_not_awaited()
            self.assertEqual(list((await session.scalars(select(TeamReplacementQueue))).all()), [])

    async def test_pending_replacement_queue_retries_when_candidate_becomes_available(self):
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            team.rotation_mode = "auto"
            team.rotation_rounds = 3
            session.add(TeamReplacementQueue(
                team_id=team.id, seat_type="premium", lane_key="premium-seat-a", next_round=2,
            ))
            await session.commit()
            replacement = AsyncMock(return_value={
                "success": True, "status": "invited", "email": "replacement@example.com",
            })

            stats = await self.service.process_pending_replacements(session, replacement)

            self.assertEqual(stats["invited"], 1)
            replacement.assert_awaited_once_with(
                team.id, "premium", "premium-seat-a", 2, frozenset(), session,
            )
            self.assertEqual(list((await session.scalars(select(TeamReplacementQueue))).all()), [])

    async def test_failed_replacement_candidate_is_skipped_on_queue_retry(self):
        async with self.session_factory() as session:
            team = await self._seed_team(session)
            team.rotation_mode = "auto"
            session.add(TeamReplacementQueue(
                team_id=team.id, seat_type="premium", lane_key="premium-seat-a", next_round=2,
            ))
            await session.commit()

            failed = AsyncMock(return_value={
                "success": False, "status": "failed", "candidate_id": 7,
                "error_code": "account_deactivated",
            })
            await self.service.process_pending_replacements(session, failed)
            queue = (await session.scalars(select(TeamReplacementQueue))).one()
            self.assertEqual(queue.excluded_entry_ids, "[7]")

            retry = AsyncMock(return_value={"success": True, "status": "no_candidate"})
            await self.service.process_pending_replacements(session, retry)
            self.assertEqual(retry.await_args.args[4], frozenset({7}))


if __name__ == "__main__":
    unittest.main()
