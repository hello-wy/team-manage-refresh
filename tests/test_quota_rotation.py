import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from fastapi import HTTPException

from app.database import Base
from app.models import (AccountPoolEntry, AccountPoolHistory, QuotaSnapshot,
                        RotationAction, RotationSeatSnapshot, Sub2apiExportRecord,
                        Team, TeamEmailMapping, TeamReplacementQueue)
from app.routes.rotation import RotationConfig, rotation_config
from app.services.account_pool import account_pool_service
from app.services.quota_rotation import (RotationDependencies, block_unverified_removal,
                                         reconcile_action, run_team)
from app.services.quota_rotation_policy import RotationDecision, next_action
from app.services.rotation_candidates import find_rotation_candidate
from app.services.quota_sync import refresh_historical_candidates
from app.utils.time_utils import get_now


def member(email="member@example.com", seat="standard", role="standard-user"):
    return SimpleNamespace(id=1, email=email, status="joined", seat_type=seat,
                           member_role=role, auto_kick_exempt=False,
                           joined_at=get_now(), created_at=get_now())


def snapshot(seat="standard", weekly=0, short=0):
    return SimpleNamespace(status="ok", observed_at=get_now(),
                           observed_seat_type=seat, weekly_remaining=weekly,
                           short_remaining=short)


def balance(standard=1, premium=1):
    return {"success": True, "balance": {
        "standard": {"known": True, "paid": 2, "remaining": standard},
        "premium": {"known": True, "paid": 1, "remaining": premium},
    }}


class RotationPolicyTests(unittest.TestCase):
    def test_short_window_exhaustion_does_not_rotate(self):
        row = member()
        self.assertIsNone(next_action([row], {row.email: snapshot(weekly=5)}, {}, balance()))

    def test_standard_weekly_exhaustion_only_upgrades(self):
        row = member()
        self.assertEqual(next_action([row], {row.email: snapshot()}, {}, balance()).action, "upgrade")

    def test_premium_weekly_exhaustion_removes_ordinary_member(self):
        row = member(seat="premium")
        self.assertEqual(next_action([row], {row.email: snapshot(seat="premium")}, {}, balance()).action, "remove")

    def test_admin_returns_only_when_standard_seat_is_free(self):
        row = member(seat="premium", role="admin")
        observed = {row.email: snapshot(seat="premium")}
        self.assertEqual(next_action([row], observed, {}, balance()).action, "return_standard")
        self.assertIsNone(next_action([row], observed, {}, balance(standard=0)))

    def test_unknown_or_old_seat_snapshot_blocks_member(self):
        row = member(seat="premium")
        old = snapshot(seat="standard")
        self.assertIsNone(next_action([row], {row.email: old}, {}, balance()))
        stale = snapshot(seat="premium")
        stale.observed_at = get_now() - timedelta(minutes=10)
        self.assertIsNone(next_action([row], {row.email: stale}, {}, balance()))

    def test_unknown_balance_pauses_team(self):
        row = member(seat="premium")
        seats = balance()
        seats["balance"]["standard"]["known"] = False
        self.assertIsNone(next_action([row], {row.email: snapshot(seat="premium")}, {}, seats))


class RotationReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_team_mode_setting_checks_old_replacement_queue(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as db:
                db.add(Team(id=1, email="owner@example.com", account_id="space",
                            access_token_encrypted="token"))
                await db.commit()
                configured = await rotation_config(1, RotationConfig(mode="auto"),
                                                   db=db, user={})
                self.assertEqual(configured["mode"], "auto")
                db.add(TeamReplacementQueue(team_id=1, seat_type="standard"))
                await db.commit()
                with self.assertRaises(HTTPException) as error:
                    await rotation_config(1, RotationConfig(mode="dry_run"),
                                          db=db, user={})
                self.assertEqual(error.exception.status_code, 409)
        finally:
            await engine.dispose()

    async def test_historical_candidate_quota_is_refreshed_by_workspace(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as db:
                db.add(Team(id=1, email="owner@example.com", account_id="space",
                            access_token_encrypted="token", rotation_mode="auto"))
                db.add(AccountPoolEntry(id=1, email="member@example.com"))
                db.add(AccountPoolHistory(account_pool_id=1, team_id=1,
                                          joined_at=get_now() - timedelta(days=8),
                                          left_at=get_now() - timedelta(days=7)))
                await db.commit()
                usage = SimpleNamespace(check_many=AsyncMock(return_value={
                    ("member@example.com", "space"): {
                        "status": "ok", "1week": {"remaining": 5}},
                }))
                self.assertEqual(await refresh_historical_candidates(db, usage), 1)
                observed = (await db.execute(select(QuotaSnapshot))).scalar_one()
                self.assertEqual(observed.team_space_id, "space")
                self.assertEqual(observed.weekly_remaining, 5)
        finally:
            await engine.dispose()

    async def test_candidate_with_in_flight_invite_is_skipped(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as db:
                team = Team(id=2, email="owner@example.com", account_id="space-2",
                            access_token_encrypted="token", rotation_mode="auto")
                db.add_all([team, Team(id=1, email="other@example.com",
                                       account_id="space-1", access_token_encrypted="token")])
                now = get_now()
                db.add_all([AccountPoolEntry(id=1, email="a@example.com", updated_at=now),
                            AccountPoolEntry(id=2, email="b@example.com", updated_at=now)])
                db.add(RotationAction(team_id=1, email="a@example.com", action_type="invite",
                                      idempotency_key="other-team:invite", status="executing"))
                await db.commit()
                candidate = await find_rotation_candidate(db, team, account_pool_service)
                self.assertEqual(candidate.email, "b@example.com")
        finally:
            await engine.dispose()

    async def test_exported_account_removal_is_explicitly_blocked(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as db:
                team = Team(id=1, email="owner@example.com", account_id="space",
                            access_token_encrypted="token", rotation_mode="auto")
                db.add(team)
                db.add(Sub2apiExportRecord(email="member@example.com", team_space_id="space"))
                await db.commit()
                result = await block_unverified_removal(db, team,
                    RotationDecision("member@example.com", "remove", "高级周额度耗尽"), {})
                self.assertEqual(result["reason"], "SUB2API_PAUSE_UNVERIFIED")
                self.assertEqual((await db.get(RotationAction, result["action_id"])).status,
                                 "blocked")
        finally:
            await engine.dispose()

    async def test_restart_reconciles_without_repeating_remote_call(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as db:
                db.add(Team(id=1, email="owner@example.com", account_id="space",
                            access_token_encrypted="token", rotation_mode="auto"))
                action = RotationAction(team_id=1, email="member@example.com",
                                        action_type="upgrade", idempotency_key="1:member:1:upgrade",
                                        status="executing")
                db.add(action)
                await db.commit()
                service = SimpleNamespace(get_team_members=AsyncMock(return_value={
                    "success": True, "members": [{"email": "member@example.com",
                                                     "seat_type": "premium", "status": "joined"}],
                }), update_member_seat_type=AsyncMock())
                self.assertEqual(await reconcile_action(db, action, service), "succeeded")
                service.update_member_seat_type.assert_not_awaited()
        finally:
            await engine.dispose()

    async def test_auto_tick_upgrades_once_after_weekly_exhaustion(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as db:
                team = Team(id=1, email="owner@example.com", account_id="space",
                            access_token_encrypted="token", rotation_mode="auto")
                db.add(team)
                db.add(TeamEmailMapping(team_id=1, email="member@example.com",
                                        status="joined", seat_type="standard",
                                        upstream_user_id="user-1", joined_at=get_now()))
                await db.commit()
                seats = balance(standard=0, premium=1)
                current = {"success": True, "seat_balance": seats,
                           "members": [{"email": "member@example.com",
                                        "seat_type": "standard", "status": "joined"}]}
                upgraded = {"success": True, "seat_balance": seats,
                            "members": [{"email": "member@example.com",
                                         "seat_type": "premium", "status": "joined"}]}
                teams = SimpleNamespace(get_team_members=AsyncMock(
                    side_effect=[current, current, upgraded]),
                    update_member_seat_type=AsyncMock(return_value={
                        "success": True, "status": "applied"}),
                )
                usage = SimpleNamespace(check_many=AsyncMock(return_value={
                    ("member@example.com", "space"): {"status": "ok",
                        "5h": {"remaining": 0}, "1week": {"remaining": 0}},
                }))
                result = await run_team(db, team, RotationDependencies(teams, usage, object()))
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual((await db.get(RotationSeatSnapshot, 1)).premium_remaining, 1)
                teams.update_member_seat_type.assert_awaited_once_with(
                    1, "user-1", "premium", "standard", db)
        finally:
            await engine.dispose()
