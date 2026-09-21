import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Team, TeamSeatHold
from app.services.team import TeamService
from app.utils.seats import calculate_seat_balance, normalize_seat_type, total_paid_seats


def capacity(standard=3, premium=1, standard_available=2, premium_available=1):
    return [
        {"type": "default", "paid": standard, "available": standard_available},
        {"type": "prolite", "paid": premium, "available": premium_available},
    ]


class SeatBalanceCalculationTests(unittest.TestCase):
    def test_total_counts_paid_standard_and_premium_not_available_or_held(self):
        rows = capacity(2, 3, 0, 1)
        rows[0]["held"] = 9
        rows.append({"type": "usage_based", "paid": 100})
        self.assertEqual(total_paid_seats(rows), 5)
        self.assertEqual(total_paid_seats(capacity(0, 0, 0, 0)), 0)

    def test_total_is_unknown_for_missing_invalid_or_duplicate_capacity(self):
        for rows in (None, [], capacity()[:1], capacity() + capacity(),
                     capacity(True, 1), capacity(-1, 1), capacity(2.5, 1)):
            with self.subTest(rows=rows):
                self.assertIsNone(total_paid_seats(rows))

    def test_official_zero_balance_blocks_even_with_local_max_members_space(self):
        result = calculate_seat_balance(capacity(2, 0, 0, 0), [{"seat_type": "default"}] * 2, [])
        self.assertEqual(result["balance"]["standard"]["remaining"], 0)
        self.assertEqual(result["balance"]["premium"]["remaining"], 0)

    def test_invites_and_unconfirmed_operations_reserve_balance(self):
        holds = [SimpleNamespace(seat_type="premium")]
        result = calculate_seat_balance(capacity(4, 3, 3, 3), [{"seat_type": "default"}],
                                        [{"seat_type": "default"}, {"seat_type": "prolite"}], holds)
        self.assertEqual(result["balance"]["standard"]["remaining"], 2)
        self.assertEqual(result["balance"]["premium"]["remaining"], 1)

    def test_pending_downgrade_does_not_release_current_seat_early(self):
        members = [{"seat_type": "prolite", "pending_seat_type": "default"}]
        result = calculate_seat_balance(capacity(1, 1, 1, 1), members, [])
        self.assertEqual(result["balance"]["premium"]["remaining"], 0)
        self.assertEqual(result["balance"]["standard"]["remaining"], 0)

    def test_missing_capacity_is_unknown_not_unlimited(self):
        result = calculate_seat_balance(None, [], [])
        self.assertFalse(result["balance"]["premium"]["known"])
        self.assertIsNone(result["balance"]["premium"]["remaining"])

    def test_aggregate_entitlement_cannot_be_used_as_typed_capacity(self):
        result = calculate_seat_balance({"seats_entitled": 10}, [], [])
        self.assertFalse(result["balance"]["standard"]["known"])

    def test_unknown_invite_type_blocks_balance_calculation(self):
        result = calculate_seat_balance(capacity(), [], [{"seat_type": None}])
        self.assertFalse(result["success"])

    def test_negative_fractional_and_boolean_capacities_are_invalid(self):
        for value in (-1, 2.5, True, "2"):
            with self.subTest(value=value):
                result = calculate_seat_balance([{"type": "default", "paid": value, "available": 0}], [], [])
                self.assertFalse(result["balance"]["standard"]["known"])

    def test_remote_overbooking_never_yields_negative_or_extra_balance(self):
        result = calculate_seat_balance(capacity(2, 1, 2, 1), [{"seat_type": "default"}] * 3, [])
        self.assertEqual(result["balance"]["standard"]["remaining"], 0)

    def test_prolite_is_premium_but_legacy_codex_is_not(self):
        self.assertEqual(normalize_seat_type("prolite"), "premium")
        self.assertEqual(normalize_seat_type("usage_based"), "unknown")


class SeatBalancePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_over_limit_batch_sends_nothing(self):
        service = TeamService()
        db = AsyncMock()
        db.get.return_value = SimpleNamespace(id=1, account_id="account", current_members=1, max_members=10)
        service.sync_team_info = AsyncMock(return_value={"success": True, "member_emails": []})
        service.get_team_seat_balance = AsyncMock(return_value={"success": True, "balance": {
            "premium": {"known": True, "remaining": 1},
        }})
        service.add_team_member = AsyncMock()
        result = await service.add_team_members(1, ["one@example.com", "two@example.com"], db, "premium")
        self.assertEqual(result["error_code"], "seat_limit_exceeded")
        self.assertFalse(result["processed"])
        service.add_team_member.assert_not_awaited()

    async def test_balance_failure_blocks_batch(self):
        service = TeamService()
        db = AsyncMock()
        db.get.return_value = SimpleNamespace(id=1, account_id="account", current_members=1, max_members=10)
        service.sync_team_info = AsyncMock(return_value={"success": True, "member_emails": []})
        service.get_team_seat_balance = AsyncMock(return_value={"success": False, "error": "timeout"})
        service.add_team_member = AsyncMock()
        result = await service.add_team_members(1, ["one@example.com"], db, "premium")
        self.assertEqual(result["error_code"], "seat_balance_unavailable")
        service.add_team_member.assert_not_awaited()


class SeatBalancePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add(Team(id=1, email="owner@example.com", account_id="account", access_token_encrypted="test"))
            await db.commit()
        self.remote = SimpleNamespace(
            get_seat_capacity=AsyncMock(return_value={"success": True, "data": {"seat_capacity": capacity()}}),
            get_members=AsyncMock(return_value={"success": True, "members": [
                {"id": "one", "email": "one@example.com", "seat_type": "default"},
                {"id": "two", "email": "two@example.com", "seat_type": "default"},
            ]}),
            get_invites=AsyncMock(return_value={"success": True, "items": []}),
            update_member_seat_type=AsyncMock(return_value={"success": True, "data": {"success": True}}),
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    def service(self):
        service = TeamService()
        service.chatgpt_service = self.remote
        service.ensure_access_token = AsyncMock(return_value="test-token")
        return service

    async def change(self, user):
        async with self.sessions() as db:
            return await self.service().update_member_seat_type(1, user, "premium", "default", db)

    async def test_two_requests_cannot_consume_the_last_seat(self):
        results = await asyncio.gather(self.change("one"), self.change("two"))
        self.assertEqual(sum(r["success"] for r in results), 1)
        self.assertEqual(sum(r.get("error_code") == "seat_limit_exceeded" for r in results), 1)
        self.remote.update_member_seat_type.assert_awaited_once()
        async with self.sessions() as db:
            # 新建服务实例仍从数据库读到预留，而不是依赖进程内计数。
            team = await db.get(Team, 1)
            balance = await self.service().get_team_seat_balance(team, db)
            self.assertEqual(balance["balance"]["premium"]["remaining"], 0)
            self.assertEqual(balance["balance"]["premium"]["reserved"], 1)

    async def test_zero_and_unknown_balance_never_reach_patch(self):
        for rows, error in [(capacity(2, 0, 0, 0), "seat_limit_exceeded"), ([], "seat_balance_unavailable")]:
            with self.subTest(error=error):
                self.remote.get_seat_capacity.return_value = {"success": True, "data": {"seat_capacity": rows}}
                result = await self.change("one")
                self.assertEqual(result["error_code"], error)
        self.remote.update_member_seat_type.assert_not_awaited()

    async def test_single_invite_cannot_bypass_zero_balance(self):
        self.remote.get_seat_capacity.return_value = {"success": True, "data": {"seat_capacity": capacity(2, 0, 0, 0)}}
        self.remote.send_invite = AsyncMock()
        service = self.service()
        service.sync_team_info = AsyncMock(return_value={"success": True, "member_emails": []})
        async with self.sessions() as db:
            result = await service.add_team_member(1, "new@example.com", db, "premium")
        self.assertEqual(result["error_code"], "seat_limit_exceeded")
        self.remote.send_invite.assert_not_awaited()

    async def test_observed_invite_is_not_double_reserved(self):
        async with self.sessions() as db:
            service = self.service()
            team = await db.get(Team, 1)
            await service._reserve_member_seat(team, "invite", "new@example.com", "premium", db)
            self.remote.get_invites.return_value = {"success": True, "items": [
                {"email_address": "new@example.com", "seat_type": "prolite", "status": 2},
            ]}
            result = await service.get_team_seat_balance(team, db)
            self.assertEqual(result["balance"]["premium"]["invited"], 1)
            self.assertEqual(result["balance"]["premium"]["reserved"], 0)
            self.assertEqual((await db.execute(select(TeamSeatHold))).scalars().all(), [])

    async def test_read_failure_preserves_unconfirmed_hold(self):
        async with self.sessions() as db:
            service = self.service()
            team = await db.get(Team, 1)
            await service._reserve_member_seat(team, "invite", "new@example.com", "premium", db)
            self.remote.get_invites.return_value = {"success": False, "error": "timeout"}
            self.assertFalse((await service.get_team_seat_balance(team, db))["success"])
            self.assertEqual(len((await db.execute(select(TeamSeatHold))).scalars().all()), 1)

    async def test_pending_hold_error_includes_hold_context(self):
        async with self.sessions() as db:
            service = self.service()
            team = await db.get(Team, 1)
            await service._reserve_member_seat(
                team, "invite", "new@example.com", "premium", db
            )
            result = await service._reserve_member_seat(
                team, "invite", "new@example.com", "premium", db
            )

            self.assertEqual(result["error_code"], "seat_operation_pending")
            self.assertEqual(result["pending_operation"], "invite")
            self.assertEqual(result["pending_seat_type"], "premium")
            self.assertIsNotNone(result["pending_since"])

    async def test_definite_rejection_releases_reservation(self):
        self.remote.update_member_seat_type.return_value = {"success": False, "status_code": 403, "error": "forbidden"}
        result = await self.change("one")
        self.assertFalse(result["success"])
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(TeamSeatHold))).scalars().all(), [])
