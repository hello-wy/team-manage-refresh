import unittest
from datetime import timedelta

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    AccountPoolEntry, AccountPoolHistory, MemberAuthorization, QuotaSnapshot,
    RotationAction, RotationMemberState, RotationSeatSnapshot, Sub2apiExportRecord,
    Team, TeamEmailMapping, TeamReplacementQueue,
)
from app.services.account_pool import account_pool_service
from app.services.account_pool_rotation import attach_rotation_status
from app.services.rotation_candidates import find_rotation_candidate
from app.utils.time_utils import get_now


class AccountPoolRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = get_now()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def team(self, db, id=1, mode="auto", standard=1, premium=1, fresh=True):
        team = Team(id=id, email=f"owner-{id}@example.com", account_id=f"space-{id}",
                    team_name=f"Team {id}", access_token_encrypted="unused",
                    rotation_mode=mode, status="active")
        db.add(team)
        db.add(RotationSeatSnapshot(team_id=id, standard_paid=4, premium_paid=2,
                                    standard_remaining=standard, premium_remaining=premium,
                                    observed_at=self.now - timedelta(minutes=0 if fresh else 6)))
        await db.flush()
        return team

    async def entry(self, db, name, order=0):
        entry = AccountPoolEntry(email=f"{name}@example.com", updated_at=self.now + timedelta(seconds=order))
        db.add(entry)
        await db.flush()
        return entry

    def quota(self, db, email, space="space-1", seat="standard", remaining=0, fresh=True):
        db.add(QuotaSnapshot(email=email, team_space_id=space, observed_seat_type=seat,
                             weekly_remaining=remaining, weekly_limit=100, status="ok",
                             observed_at=self.now - timedelta(minutes=0 if fresh else 6)))

    async def view(self, db, *entries):
        rows = [{"id": e.id, "email": e.email} for e in entries]
        return {row["email"]: row["rotation"] for row in await attach_rotation_status(db, rows)}

    async def test_full_pool_fifo_is_independent_of_page_and_does_not_double_assign(self):
        async with self.sessions() as db:
            await self.team(db, 1)
            await self.team(db, 2)
            first = await self.entry(db, "first", 0)
            second = await self.entry(db, "second", 1)
            third = await self.entry(db, "third", 2)
            await db.commit()
            all_rows = await self.view(db, first, second, third)
            page = await self.view(db, second)
            self.assertEqual(all_rows[first.email]["team_id"], 1)
            self.assertEqual(all_rows[second.email]["team_id"], 2)
            self.assertEqual(page[second.email], all_rows[second.email])
            self.assertEqual(all_rows[third.email]["label"], "等待排队")
            self.assertIsNone(all_rows[third.email]["team_id"])

    async def test_candidate_restrictions_match_executor(self):
        async with self.sessions() as db:
            team = await self.team(db)
            entries = [await self.entry(db, name, i) for i, name in enumerate(
                ("cooldown", "exported", "uncertain", "old-quota", "zero-quota", "eligible"))]
            db.add(TeamEmailMapping(team_id=1, email=entries[0].email, status="removed",
                                    last_invited_at=self.now - timedelta(days=1)))
            db.add(Sub2apiExportRecord(email=entries[1].email, team_space_id="space-1", team_id=1))
            db.add(MemberAuthorization(team_id=1, account_id="space-1", email=entries[2].email,
                                       sub2api_import_uncertain=True))
            for entry in entries[3:]:
                db.add(AccountPoolHistory(account_pool_id=entry.id, team_id=1,
                                           joined_at=self.now - timedelta(days=10)))
            self.quota(db, entries[3].email, remaining=80, fresh=False)
            self.quota(db, entries[4].email, remaining=0)
            self.quota(db, entries[5].email, remaining=80)
            await db.commit()
            actual = await find_rotation_candidate(db, team, account_pool_service)
            views = await self.view(db, *entries)
            self.assertEqual(actual.email, entries[5].email)
            self.assertEqual(views[actual.email]["team_id"], 1)
            for entry in entries[:5]:
                self.assertIsNone(views[entry.email]["team_id"])
                self.assertEqual(views[entry.email]["label"], "等待资格恢复")
            self.assertIn("7 天", views[entries[0].email]["reason"])

    async def test_stale_seats_or_no_rotation_do_not_invent_a_target(self):
        async with self.sessions() as db:
            await self.team(db, fresh=False)
            await self.team(db, id=2, mode="off")
            entry = await self.entry(db, "candidate")
            await db.commit()
            view = (await self.view(db, entry))[entry.email]
            self.assertIsNone(view["team_id"])
            self.assertIn("最新席位", view["reason"])

    async def test_dry_run_does_not_claim_candidates_from_auto_teams(self):
        async with self.sessions() as db:
            await self.team(db, id=1, mode="dry_run")
            await self.team(db, id=2)
            first = await self.entry(db, "first", 0)
            second = await self.entry(db, "second", 1)
            await db.commit()
            views = await self.view(db, first, second)
            self.assertEqual(views[first.email]["team_id"], 2)
            self.assertEqual(views[first.email]["label"], "预计加入")
            self.assertEqual(views[second.email]["team_id"], 1)
            self.assertEqual(views[second.email]["label"], "仅预览")

    async def test_pending_actions_block_team_and_reserve_account(self):
        async with self.sessions() as db:
            await self.team(db, id=1)
            await self.team(db, id=2)
            first = await self.entry(db, "pending")
            second = await self.entry(db, "available", 1)
            db.add(RotationAction(team_id=1, email=first.email, action_type="invite",
                                   status="reconciling", idempotency_key="pending-invite"))
            await db.commit()
            views = await self.view(db, first, second)
            self.assertEqual(views[first.email]["label"], "邀请确认中")
            self.assertEqual(views[first.email]["team_id"], 1)
            self.assertEqual(views[second.email]["team_id"], 2)

    async def test_legacy_replacement_queue_preserves_seat_type_and_fifo(self):
        async with self.sessions() as db:
            await self.team(db, mode="off")
            for kind in ("premium", "standard"):
                db.add(TeamReplacementQueue(team_id=1, seat_type=kind))
            first = await self.entry(db, "first", 0)
            second = await self.entry(db, "second", 1)
            await db.commit()
            views = await self.view(db, first, second)
            self.assertIn("高级席位", views[first.email]["reason"])
            self.assertIn("标准席位", views[second.email]["reason"])
            self.assertEqual(views[first.email]["team_id"], 1)

    async def test_member_stages_do_not_pretend_to_move_between_teams(self):
        async with self.sessions() as db:
            await self.team(db, id=1)
            await self.team(db, id=2)
            entries = []
            cases = [("standard", "standard", None, 0, "待升高级"),
                     ("premium", "premium", None, 0, "待退出后匹配"),
                     ("owner", "premium", "account-owner", 0, "待降标准"),
                     ("using", "standard", None, 50, "继续使用"),
                     ("exported", "premium", None, 0, "轮转受阻")]
            for name, seat, role, remaining, expected in cases:
                entry = await self.entry(db, name)
                entries.append(entry)
                db.add(TeamEmailMapping(team_id=1, email=entry.email, status="joined",
                                        seat_type=seat, member_role=role))
                self.quota(db, entry.email, seat=seat, remaining=remaining)
            db.add(Sub2apiExportRecord(email=entries[-1].email, team_space_id="space-1", team_id=1))
            await db.commit()
            views = await self.view(db, *entries)
            for entry, case in zip(entries, cases):
                self.assertEqual(views[entry.email]["label"], case[-1])
                self.assertEqual(views[entry.email]["team_id"], None if case[0] == "premium" else 1)

    async def test_legacy_queue_without_fresh_capacity_only_predicts_first_candidate(self):
        async with self.sessions() as db:
            await self.team(db, mode="off", fresh=False)
            db.add_all([TeamReplacementQueue(team_id=1, seat_type="premium"),
                        TeamReplacementQueue(team_id=1, seat_type="standard")])
            first = await self.entry(db, "first", 0)
            second = await self.entry(db, "second", 1)
            await db.commit()
            views = await self.view(db, first, second)
            self.assertEqual(views[first.email]["label"], "候选补位")
            self.assertEqual(views[first.email]["team_id"], 1)
            self.assertIn("等待席位余额确认", views[first.email]["reason"])
            self.assertIsNone(views[second.email]["team_id"])

    async def test_queue_reports_recent_execution_blocker_without_fake_countdown(self):
        async with self.sessions() as db:
            await self.team(db, mode="off", fresh=False)
            item = TeamReplacementQueue(team_id=1, seat_type="standard", last_attempt_at=self.now,
                                        last_error_code="seat_limit_exceeded")
            db.add_all([item, TeamReplacementQueue(team_id=1, seat_type="premium")])
            first = await self.entry(db, "first")
            second = await self.entry(db, "second", 1)
            await db.commit()
            for code, expected in (("seat_limit_exceeded", "等待标准席位空位"),
                                   ("seat_balance_unavailable", "等待席位余额确认"),
                                   ("ghost_success", "邀请未在上游确认"),
                                   ("upstream_failed", "上次邀请失败")):
                item.last_error_code = code
                await db.commit()
                views = await self.view(db, first, second)
                self.assertEqual(views[first.email]["label"], "候选补位")
                self.assertIn(expected, views[first.email]["reason"])
                self.assertIsNone(views[first.email]["replacement_at"])
                self.assertIsNone(views[second.email]["team_id"])
            item.last_attempt_at = self.now - timedelta(minutes=6)
            await db.commit()
            view = (await self.view(db, first))[first.email]
            self.assertIn("等待席位余额确认", view["reason"])
            self.assertNotIn("邀请失败", view["reason"])

    async def test_zero_capacity_preserves_head_candidate_and_blocks_later_queue(self):
        async with self.sessions() as db:
            await self.team(db, mode="off", standard=0, premium=2)
            db.add_all([TeamReplacementQueue(team_id=1, seat_type="standard"),
                        TeamReplacementQueue(team_id=1, seat_type="premium")])
            first = await self.entry(db, "first")
            second = await self.entry(db, "second", 1)
            await db.commit()
            views = await self.view(db, first, second)
            self.assertEqual(views[first.email]["label"], "候选补位")
            self.assertIn("等待标准席位空位", views[first.email]["reason"])
            self.assertIsNone(views[first.email]["replacement_at"])
            self.assertIsNone(views[second.email]["team_id"])

    async def test_invited_conflicted_and_exempt_members(self):
        async with self.sessions() as db:
            await self.team(db, mode="off")
            await self.team(db, id=2, mode="off")
            invited = await self.entry(db, "invited")
            conflict = await self.entry(db, "conflict")
            exempt = await self.entry(db, "exempt")
            db.add_all([
                TeamEmailMapping(team_id=1, email=invited.email, status="invited"),
                TeamEmailMapping(team_id=1, email=conflict.email, status="joined"),
                TeamEmailMapping(team_id=2, email=conflict.email, status="joined"),
                TeamEmailMapping(team_id=1, email=exempt.email, status="joined", auto_kick_exempt=True),
            ])
            await db.commit()
            views = await self.view(db, invited, conflict, exempt)
            self.assertEqual(views[invited.email]["label"], "待接受邀请")
            self.assertEqual(views[invited.email]["team_id"], 1)
            self.assertEqual(views[conflict.email]["label"], "归属冲突")
            self.assertIsNone(views[conflict.email]["team_id"])
            self.assertEqual(views[exempt.email]["label"], "保留当前 Team")

    async def test_preview_is_read_only_and_uses_batched_queries(self):
        async with self.sessions() as db:
            await self.team(db)
            entries = [await self.entry(db, f"member-{i}", i) for i in range(50)]
            await db.commit()
            statements = []
            def capture(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)
            event.listen(self.engine.sync_engine, "before_cursor_execute", capture)
            try:
                await self.view(db, *entries)
            finally:
                event.remove(self.engine.sync_engine, "before_cursor_execute", capture)
            self.assertEqual(len(statements), 11)
            self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements))
            self.assertFalse(db.new or db.dirty or db.deleted)
            self.assertEqual((await db.execute(select(RotationAction))).scalars().all(), [])

    async def test_scheduled_replacement_counts_down_to_member_exit_without_inviting(self):
        async with self.sessions() as db:
            await self.team(db, mode="off", standard=0, fresh=False)
            candidate = await self.entry(db, "candidate")
            deadline = self.now + timedelta(minutes=25)
            db.add_all([
                TeamEmailMapping(team_id=1, email="leaving@example.com", status="joined",
                                 seat_type="standard", upstream_user_id="user-1", member_role="standard-user", auto_kick_at=deadline),
                TeamEmailMapping(team_id=1, email="owner-1@example.com", status="joined",
                                 seat_type="premium", upstream_user_id="owner", auto_kick_at=self.now,
                                 member_role="account-owner"),
            ])
            await db.commit()
            view = (await self.view(db, candidate))[candidate.email]
            self.assertEqual(view["label"], "候选补位")
            self.assertEqual(view["kind"], "candidate")
            self.assertTrue(view["scheduled_replacement"])
            self.assertTrue(view["replacement_at"].startswith(deadline.isoformat()))
            self.assertEqual((await db.execute(select(RotationAction))).scalars().all(), [])
            self.assertFalse(db.new or db.dirty)

    async def test_live_vacancy_has_priority_over_future_scheduled_replacement(self):
        async with self.sessions() as db:
            await self.team(db, 1, mode="off")
            await self.team(db, 2)
            first = await self.entry(db, "first")
            second = await self.entry(db, "second", 1)
            db.add(TeamEmailMapping(team_id=1, email="leaving@example.com", status="joined",
                                   upstream_user_id="user-1", seat_type="premium", member_role="standard-user",
                                   auto_kick_at=self.now + timedelta(hours=1)))
            await db.commit()
            views = await self.view(db, first, second)
            self.assertEqual(views[first.email]["team_id"], 2)
            self.assertEqual(views[second.email]["team_id"], 1)
            self.assertIn("高级席位", views[second.email]["reason"])
            self.assertIn("replacement_at", views[second.email])
