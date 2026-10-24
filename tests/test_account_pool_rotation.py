import unittest
from datetime import timedelta

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    AccountPoolEntry, QuotaSnapshot, RotationAction, RotationMemberState, RotationSeatSnapshot, Sub2apiExportRecord,
    Team, TeamEmailMapping, TeamReplacementQueue,
)
from app.services.account_pool_rotation import attach_rotation_status
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

    async def team(self, db, id=1, mode="auto", standard=1, premium=1):
        team = Team(id=id, email=f"owner-{id}@example.com", account_id=f"space-{id}",
                    team_name=f"Team {id}", access_token_encrypted="unused",
                    rotation_mode=mode, status="active")
        db.add(team)
        db.add(RotationSeatSnapshot(team_id=id, standard_paid=4, premium_paid=2,
                                    standard_remaining=standard, premium_remaining=premium,
                                    observed_at=self.now))
        await db.flush()
        return team

    async def entry(self, db, name, order=0):
        entry = AccountPoolEntry(email=f"{name}@example.com", updated_at=self.now + timedelta(seconds=order))
        db.add(entry)
        await db.flush()
        return entry

    def quota(self, db, email, space="space-1", seat="standard", remaining=0):
        db.add(QuotaSnapshot(email=email, team_space_id=space, observed_seat_type=seat,
                             weekly_remaining=remaining, weekly_limit=100, status="ok",
                             observed_at=self.now))

    async def view(self, db, *entries):
        rows = [{"id": e.id, "email": e.email} for e in entries]
        return {row["email"]: row["rotation"] for row in await attach_rotation_status(db, rows)}

    async def test_idle_accounts_are_never_predicted_to_join(self):
        async with self.sessions() as db:
            await self.team(db, id=1, mode="auto", standard=2)
            await self.team(db, id=2, mode="off")
            db.add(TeamReplacementQueue(team_id=2, seat_type="standard"))
            idle = await self.entry(db, "idle")
            await db.commit()
            view = (await self.view(db, idle))[idle.email]
            self.assertEqual(view["label"], "空闲")
            self.assertEqual(view["kind"], "idle")
            self.assertIsNone(view["team_id"])
            self.assertIn("不会自动邀请", view["reason"])
            self.assertNotIn("replacement_at", view)

    async def test_pending_actions_show_upstream_confirmation(self):
        async with self.sessions() as db:
            await self.team(db, id=1)
            pending = await self.entry(db, "pending")
            idle = await self.entry(db, "idle", 1)
            db.add(RotationAction(team_id=1, email=pending.email, action_type="invite",
                                   status="reconciling", idempotency_key="pending-invite"))
            await db.commit()
            views = await self.view(db, pending, idle)
            self.assertEqual(views[pending.email]["label"], "")
            self.assertIn("邀请确认中", views[pending.email]["reason"])
            self.assertEqual(views[pending.email]["team_id"], 1)
            self.assertEqual(views[idle.email]["label"], "空闲")

    async def test_member_stages_stay_in_their_team(self):
        async with self.sessions() as db:
            await self.team(db, id=1)
            entries = []
            cases = [("standard", "standard", None, 0, "待升高级"),
                     ("premium", "premium", None, 0, "等待退出"),
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
            for entry, case, label in zip(entries, cases, ["待下线", "待下线", "", "待下线", ""]):
                self.assertEqual(views[entry.email]["label"], label)
                expected = "定时下线" if entry.email in ("standard@example.com", "using@example.com") else case[-1]
                self.assertEqual(views[entry.email]["detail"], expected)
                self.assertIn(expected, views[entry.email]["reason"])
                self.assertEqual(views[entry.email]["team_id"], 1)
            self.assertIn("不会自动补位", views[entries[1].email]["reason"])

    async def test_waiting_for_quota_retains_reason_without_rotation_badge(self):
        async with self.sessions() as db:
            await self.team(db)
            entry = await self.entry(db, "waiting-for-quota")
            db.add(TeamEmailMapping(team_id=1, email=entry.email, status="joined",
                                    seat_type="premium"))
            db.add(RotationMemberState(team_id=1, email=entry.email, phase="wait_reset"))
            self.quota(db, entry.email, seat="premium", remaining=0)
            await db.commit()
            view = (await self.view(db, entry))[entry.email]
            self.assertEqual(view["status"], "")
            self.assertEqual(view["label"], "")
            self.assertEqual(view["detail"], "等待额度恢复")
            self.assertIn("周额度恢复前保持当前状态", view["reason"])

    async def test_invited_conflicted_non_participating_and_pending_members(self):
        async with self.sessions() as db:
            await self.team(db, mode="off")
            await self.team(db, id=2, mode="off")
            invited = await self.entry(db, "invited")
            conflict = await self.entry(db, "conflict")
            exempt = await self.entry(db, "exempt")
            kept = await self.entry(db, "kept")
            db.add_all([
                TeamEmailMapping(team_id=1, email=invited.email, status="invited"),
                TeamEmailMapping(team_id=1, email=conflict.email, status="joined"),
                TeamEmailMapping(team_id=2, email=conflict.email, status="joined"),
                TeamEmailMapping(team_id=1, email=exempt.email, status="joined", seat_type="premium", auto_kick_enabled=False),
                TeamEmailMapping(team_id=2, email=kept.email, status="joined", upstream_user_id="user-kept"),
            ])
            await db.commit()
            views = await self.view(db, invited, conflict, exempt, kept)
            self.assertEqual(views[invited.email]["label"], "待接受邀请")
            self.assertEqual(views[invited.email]["team_id"], 1)
            self.assertEqual(views[conflict.email]["label"], "")
            self.assertIn("归属冲突", views[conflict.email]["reason"])
            self.assertIsNone(views[conflict.email]["team_id"])
            self.assertEqual(views[exempt.email]["label"], "所有者")
            self.assertIn("未参与自动下线", views[exempt.email]["reason"])
            self.assertEqual(views[kept.email]["label"], "")
            self.assertIn("席位待确认", views[kept.email]["reason"])

    async def test_timed_exit_counts_down_without_scheduling_a_replacement(self):
        async with self.sessions() as db:
            await self.team(db, mode="off", standard=0)
            leaving = await self.entry(db, "leaving")
            idle = await self.entry(db, "idle", 1)
            deadline = self.now + timedelta(minutes=25)
            (await db.get(Team, 1)).expires_at = deadline
            db.add(TeamEmailMapping(team_id=1, email=leaving.email, status="joined",
                                    standard_auto_kick_enabled=True, seat_type="standard",
                                    upstream_user_id="user-1", member_role="standard-user",
                                    auto_kick_at=deadline))
            await db.commit()
            views = await self.view(db, leaving, idle)
            self.assertEqual(views[leaving.email]["label"], "待下线")
            self.assertIn("定时下线", views[leaving.email]["reason"])
            self.assertTrue(views[leaving.email]["exit_at"].startswith(deadline.isoformat()))
            self.assertIn("Team 到期下线", views[leaving.email]["reason"])
            self.assertEqual(views[idle.email]["label"], "空闲")

    async def test_status_is_read_only_and_uses_batched_queries(self):
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
            self.assertEqual(len(statements), 6)
            self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements))
            self.assertFalse(db.new or db.dirty or db.deleted)
            self.assertEqual((await db.execute(select(RotationAction))).scalars().all(), [])


if __name__ == "__main__":
    unittest.main()
