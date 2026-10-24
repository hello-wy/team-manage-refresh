"""质保兑换成员在所有自动清退入口保持免踢，普通成员与手动删除不受影响。"""
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import RedemptionCode, RedemptionRecord, RotationAction, Setting, Team, TeamEmailMapping
from app.services.member_auto_kick import MemberAutoKickService
from app.services.member_departure import AccountPoolMemberExit, MemberExitTarget
from app.services.quota_rotation import _execute_remote, load_rotation_data, reconcile_action
from app.services.quota_rotation_policy import next_action
from app.services.redemption import RedemptionService
from app.services.settings import settings_service
from app.services.team import TeamService
from app.services.warranty import WarrantyService
from app.services.warranty_member_protection import is_warranty_member
from app.utils.time_utils import get_now

EMAIL = "member@example.com"


class WarrantyMemberProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings_service.clear_cache()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        # 与生产保持一致，确保新兑换记录在禁用 autoflush 时也生效。
        self.db = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)()
        self.team = Team(id=1, email="owner@example.com", account_id="space",
                         access_token_encrypted="unused", rotation_mode="off")
        self.mapping = TeamEmailMapping(
            team_id=1, email=EMAIL, status="joined", upstream_user_id="user",
            member_role="standard-user", seat_type="premium",
            joined_at=get_now() - timedelta(days=40), created_at=get_now() - timedelta(days=40),
            auto_kick_enabled=True, auto_kick_exempt=False,
            auto_kick_at=get_now() - timedelta(days=39),
        )
        self.db.add_all([self.team, self.mapping])
        await self.db.commit()
        self.teams = TeamService()
        self.scanner = MemberAutoKickService()
        self.warranty = WarrantyService()
        self.warranty.team_service = SimpleNamespace(
            _normalize_member_email=TeamService._normalize_member_email,
            remove_invite_or_member=AsyncMock(return_value={"success": True}),
        )

    async def asyncTearDown(self):
        settings_service.clear_cache()
        await self.db.close()
        await self.engine.dispose()

    async def redeem(self, *, code="WARRANTY", warranty=True, email=EMAIL, record=True,
                     used=True, record_flag=False, team_id=1):
        obj = RedemptionCode(code=code, has_warranty=warranty, warranty_days=30,
                             status="used" if used else "unused",
                             used_team_id=team_id if used else None,
                             used_by_email=email if used else None,
                             used_at=get_now() - timedelta(days=40) if used else None,
                             warranty_expires_at=get_now() - timedelta(days=10) if used else None)
        self.db.add(obj)
        if record:
            self.db.add(RedemptionRecord(code=code, email=email, team_id=team_id,
                                         account_id="space", is_warranty_redemption=record_flag,
                                         redeemed_at=get_now() - timedelta(days=40)))
        await self.db.commit()
        return obj

    def assert_protected(self):
        self.assertFalse(self.mapping.auto_kick_enabled)
        self.assertTrue(self.mapping.auto_kick_exempt)
        self.assertIsNone(self.mapping.auto_kick_at)
        self.assertEqual(self.mapping.auto_kick_failures, 0)
        self.assertIsNone(self.mapping.auto_kick_retry_at)
        self.assertIsNone(self.mapping.auto_kick_last_error)

    async def sync(self):
        await self.teams._reconcile_team_email_mappings(
            1, {EMAIL}, set(), self.db, joined_members={EMAIL: {
                "id": "user", "role": "standard-user", "seat_type": "premium",
                "created_time": self.mapping.joined_at.isoformat() if self.mapping.joined_at else None,
            }},
        )
        await self.db.commit()

    async def test_expired_legacy_warranty_member_is_excluded_before_sync(self):
        await self.redeem(record_flag=False)
        delete = AsyncMock()
        self.assertEqual((await self.scanner.run_due_members(self.db, delete))["scanned"], 0)
        delete.assert_not_awaited()

    async def test_sync_clears_existing_schedule_and_retry(self):
        await self.redeem()
        self.mapping.auto_kick_failures = 2
        self.mapping.auto_kick_last_error = "旧错误"
        self.mapping.auto_kick_retry_at = get_now()
        await self.sync()
        self.assert_protected()

    async def test_rejoin_and_changed_user_id_keep_protection(self):
        await self.redeem()
        for status, user_id in (("removed", "user"), ("invited", None), ("joined", "old-user")):
            self.mapping.status = status
            self.mapping.upstream_user_id = user_id
            await self.db.commit()
            await self.sync()
            self.assert_protected()

    async def test_first_sync_and_invite_are_protected(self):
        await self.redeem()
        await self.db.delete(self.mapping)
        await self.db.commit()
        self.mapping = await self.teams.upsert_team_email_mapping(1, EMAIL, "invited", self.db)
        self.assert_protected()
        await self.db.commit()
        await self.sync()
        self.assert_protected()

    async def test_fresh_sync_without_local_mapping_is_protected(self):
        await self.redeem()
        await self.db.delete(self.mapping)
        await self.db.commit()
        await self.teams._reconcile_team_email_mappings(1, {EMAIL}, set(), self.db,
            joined_members={EMAIL: {"id": "user", "role": "standard-user"}})
        await self.db.commit()
        self.mapping = (await self.db.scalars(select(TeamEmailMapping))).one()
        self.assert_protected()

    async def test_toggle_cannot_enable_and_team_hours_cannot_restore_schedule(self):
        await self.redeem()
        result = await self.scanner.update_member_participation(1, "user", True, self.db)
        self.assertFalse(result["success"])
        self.assertIn("质保", result["error"])
        await self.scanner.update_team_hours(1, 1, self.db)
        self.assert_protected()

    async def test_normal_and_unused_warranty_codes_do_not_exempt_member(self):
        await self.redeem(warranty=False)
        await self.redeem(code="UNUSED", record=False, used=False)
        await self.sync()
        self.assertTrue(self.mapping.auto_kick_enabled)
        self.assertFalse(self.mapping.auto_kick_exempt)
        self.assertEqual(len(await self.scanner._due_members(self.db)), 1)

    async def test_case_insensitive_legacy_code_binding_without_record_is_protected(self):
        await self.redeem(email=" MEMBER@EXAMPLE.COM ", record=False)
        self.assertTrue(await is_warranty_member(self.db, 1, EMAIL))
        await self.sync()
        self.assert_protected()

    async def test_redemption_history_stays_protected_after_code_binding_changes(self):
        code = await self.redeem(record_flag=True)
        code.has_warranty = False
        code.used_by_email = "other@example.com"
        await self.db.commit()
        self.assertTrue(await is_warranty_member(self.db, 1, EMAIL))
        self.assertFalse(await is_warranty_member(self.db, 2, EMAIL))

    async def test_queued_auto_kick_checks_new_redemption_before_callback(self):
        due = await self.scanner._due_members(self.db)
        await self.redeem()
        self.scanner._due_members = AsyncMock(return_value=due)
        delete = AsyncMock()
        await self.scanner.run_due_members(self.db, delete)
        delete.assert_not_awaited()

    async def test_self_departure_rechecks_after_login(self):
        remote = SimpleNamespace(delete_workspace_user=AsyncMock())
        departure = AccountPoolMemberExit(authorization=None, remote=remote)

        async def login(target):
            await self.redeem()
            return {"access_token": "token"}

        departure._login = AsyncMock(side_effect=login)
        result = await departure(MemberExitTarget(self.team, "user", EMAIL, self.db))
        self.assertFalse(result["success"])
        self.assertFalse(result.get("admin_removal_required"))
        remote.delete_workspace_user.assert_not_awaited()
        self.assert_protected()

    async def test_quota_rotation_returns_to_standard_instead_of_removing(self):
        await self.redeem()
        mappings, _, _ = await load_rotation_data(self.db, self.team)
        snapshot = SimpleNamespace(status="ok", observed_at=get_now(),
            observed_seat_type="premium", weekly_remaining=0, short_remaining=0)
        balance = {"success": True, "balance": {
            "standard": {"known": True, "paid": 2, "remaining": 1},
            "premium": {"known": True, "paid": 1, "remaining": 0}}}
        self.assertEqual(next_action(mappings, {EMAIL: snapshot}, {}, balance).action, "return_standard")
        self.assert_protected()

    async def test_queued_quota_removal_rechecks_warranty(self):
        await self.redeem()
        remote = SimpleNamespace(delete_team_member=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "受保护"):
            await _execute_remote(self.db, self.team,
                SimpleNamespace(action_type="remove", email=EMAIL),
                mapping=self.mapping, team_service=remote)
        remote.delete_team_member.assert_not_awaited()

    async def test_pending_quota_removal_is_cancelled_without_blocking_rotation(self):
        await self.redeem()
        action = RotationAction(team_id=1, email=EMAIL, action_type="remove",
                                status="reconciling", idempotency_key="queued-remove")
        self.db.add(action)
        await self.db.commit()
        remote = SimpleNamespace(get_team_members=AsyncMock())
        self.assertEqual(await reconcile_action(self.db, action, remote), "cancelled")
        remote.get_team_members.assert_not_awaited()

    async def test_expired_normal_code_cannot_remove_warranty_member(self):
        await self.redeem()
        await self.redeem(code="NORMAL", warranty=False)
        scan = await self.warranty.scan_expired_warranty_codes(self.db)
        self.assertTrue(scan["success"], scan.get("error"))
        self.assertEqual(scan["total"], 0)
        for code in ("NORMAL", "WARRANTY"):
            result = await self.warranty.kick_and_destroy_expired_warranty_code(self.db, code)
            self.assertEqual(result["skip_reason"], "warranty_member")
        self.warranty.team_service.remove_invite_or_member.assert_not_awaited()
        self.assertEqual(len((await self.db.scalars(select(RedemptionCode))).all()), 2)

    async def test_admin_invite_and_unauthorized_jobs_skip_legacy_warranty_member(self):
        await self.redeem(record=False)
        self.db.add_all([
            Setting(key="auto_kick_admin_invited_enabled", value="true"),
            Setting(key="auto_kick_admin_invited_enabled_since", value=(get_now() - timedelta(days=60)).isoformat()),
            Setting(key="auto_kick_admin_invited_period_days", value="1"),
            Setting(key="auto_kick_unauthorized_enabled", value="true"),
            Setting(key="auto_kick_unauthorized_enabled_since", value=(get_now() - timedelta(days=60)).isoformat()),
        ])
        self.mapping.is_admin_invited = True
        await self.db.commit()
        scan = await self.warranty.scan_admin_invited_expired_members(self.db)
        self.assertTrue(scan["success"], scan.get("error"))
        self.assertEqual(scan["total"], 0)
        result = await self.warranty.kick_admin_invited_expired_member(self.db, 1, EMAIL)
        self.assertEqual(result["skip_reason"], "warranty_member")
        self.mapping.is_admin_invited = False
        await self.db.commit()
        scan = await self.warranty.scan_unauthorized_team_members(self.db)
        self.assertTrue(scan["success"], scan.get("error"))
        self.assertEqual(scan["total"], 0)
        result = await self.warranty.kick_unauthorized_team_member(self.db, 1, EMAIL)
        self.assertEqual(result["skip_reason"], "warranty_member")
        self.warranty.team_service.remove_invite_or_member.assert_not_awaited()

    async def test_legacy_use_code_immediately_protects_mapping(self):
        await self.redeem(record=False, used=False)
        result = await RedemptionService().use_code("WARRANTY", EMAIL, 1, "space", self.db)
        self.assertTrue(result["success"], result.get("error"))
        self.assert_protected()

    async def test_manual_delete_still_works_for_protected_member(self):
        await self.redeem()
        self.teams.ensure_access_token = AsyncMock(return_value="owner-token")
        self.teams._delete_as_member = AsyncMock(return_value={"success": True})
        self.teams._member_absent_in_snapshot = AsyncMock(return_value=True)
        self.teams._complete_member_removal = AsyncMock(return_value={"success": True})
        result = await self.teams.delete_team_member(1, "user", self.db, email=EMAIL)
        self.assertTrue(result["success"])
        self.teams._delete_as_member.assert_awaited_once()
