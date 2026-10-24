"""真实数据库覆盖确认计时、席位状态与 Team 到期清退。"""
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import RedemptionCode, RedemptionRecord, Team, TeamEmailMapping
from app.services.member_auto_kick import MemberAutoKickService, schedule_payload
from app.services.member_departure import AccountPoolMemberExit, MemberExitTarget
from app.services.member_lifecycle import activate_joined_redemptions, service_start
from app.services.member_seat_status import decorate_seat_status
from app.services.settings import settings_service
from app.services.team import TeamService
from app.services.warranty import WarrantyService
from app.services.warranty_member_protection import protect_team_mappings
from app.utils.time_utils import get_now

EMAIL = 'member@example.com'


class MemberSeatLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings_service.clear_cache()
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)()
        self.now = get_now()
        self.team = Team(id=1, email='owner@example.com', account_id='space',
                         access_token_encrypted='unused', rotation_mode='off',
                         expires_at=self.now + timedelta(days=20), member_auto_kick_hours=2)
        self.mapping = TeamEmailMapping(team_id=1, email=EMAIL, status='invited', source='redeem',
                                        last_invited_at=self.now - timedelta(days=3))
        self.code = RedemptionCode(code='CODE', has_warranty=True, warranty_days=30, status='used',
                                   used_at=self.now - timedelta(days=3), used_by_email=EMAIL, used_team_id=1)
        self.record = RedemptionRecord(code='CODE', email=EMAIL, team_id=1, account_id='space',
                                       redeemed_at=self.now - timedelta(days=3),
                                       is_warranty_redemption=True, activation_pending=True)
        self.db.add_all([self.team, self.mapping, self.code, self.record])
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()
        settings_service.clear_cache()

    async def joined(self, seat='default'):
        await TeamService()._reconcile_team_email_mappings(1, {EMAIL}, set(), self.db,
            joined_members={EMAIL: {'id': 'user', 'role': 'standard-user', 'seat_type': seat,
                                   'created_time': self.now.isoformat()}})
        await self.db.commit()

    async def view(self):
        member = dict(email=EMAIL, role=self.mapping.member_role, status=self.mapping.status,
                      seat_type=self.mapping.seat_type, **schedule_payload(self.mapping))
        return (await decorate_seat_status(self.db, self.team, [member]))[0]

    async def test_pending_invite_has_no_warranty_expiry_or_departure(self):
        await activate_joined_redemptions(self.db, 1, [self.mapping])
        await protect_team_mappings(self.db, 1, [self.mapping])
        member = await self.view()
        self.assertEqual(member['seat_status'], 'invited')
        self.assertIsNone(member['auto_kick_at'])
        self.assertIsNone(member['warranty_expires_at'])
        self.assertTrue(self.record.activation_pending)

    async def test_join_confirmation_starts_30_days_once_and_uses_team_expiry(self):
        await self.joined()
        self.assertFalse(self.record.activation_pending)
        self.assertEqual(self.record.service_started_at, self.now)
        self.assertEqual(self.code.warranty_expires_at, self.now + timedelta(days=30))
        self.assertEqual(self.mapping.auto_kick_at, self.team.expires_at)
        self.assertFalse(self.mapping.auto_kick_enabled)
        member = await self.view()
        self.assertEqual(member['seat_status'], 'warranty')
        self.assertNotEqual(member['warranty_expires_at'], member['auto_kick_at'])
        await self.joined()
        self.assertEqual(self.record.service_started_at, self.now)

    async def test_invite_does_not_inherit_old_upstream_join_time(self):
        await TeamService()._reconcile_team_email_mappings(1, {EMAIL}, set(), self.db,
            joined_members={EMAIL: {'id': 'user', 'role': 'standard-user',
                                   'created_time': (self.now - timedelta(days=100)).isoformat()}})
        self.assertGreaterEqual(self.mapping.joined_at, self.now)
        self.assertEqual(self.record.service_started_at, self.mapping.joined_at)

    async def test_first_use_preserves_historical_warranty_deadline(self):
        expiry = self.now + timedelta(days=4)
        self.code.warranty_expires_at = expiry
        self.db.add(RedemptionRecord(code='CODE', email='old@example.com', team_id=1,
            account_id='space', redeemed_at=self.now - timedelta(days=26)))
        await self.db.commit()
        await self.joined()
        self.assertEqual(self.code.warranty_expires_at, expiry)

    async def test_refresh_mode_waits_for_new_confirmation(self):
        await settings_service.update_setting(self.db, 'warranty_expiration_mode', 'refresh_on_redeem')
        self.assertIsNone(await WarrantyService()._get_warranty_start_time(self.db, self.code))
        await self.joined()
        self.assertEqual(self.code.warranty_expires_at, self.now + timedelta(days=30))

    async def test_pending_warranty_is_valid_before_service_starts(self):
        warranty = WarrantyService()
        expiry = await warranty._resolve_warranty_expiry_date(self.db, self.code)
        self.assertIsNone(expiry)
        self.assertTrue(warranty._is_warranty_valid(self.code, expiry))

    async def test_legacy_refresh_retains_used_at(self):
        self.record.activation_pending = False
        self.code.used_at = self.now - timedelta(days=1)
        self.assertEqual(service_start(self.code, [self.record], 'refresh_on_redeem'), self.code.used_at)

    async def test_reusable_code_uses_each_record_confirmation(self):
        self.code.reusable_by_seat = True
        await self.joined()
        earlier = RedemptionRecord(code='CODE', email='other@example.com', team_id=1,
            account_id='space', redeemed_at=self.now - timedelta(days=15),
            service_started_at=self.now - timedelta(days=10), activation_pending=False)
        self.assertEqual(service_start(self.code, [earlier, self.record], 'first_use', self.record), self.now)
        self.assertEqual((await self.view())['warranty_expires_at'], (self.now + timedelta(days=30)).isoformat())

    async def test_normal_pending_code_is_not_expired_by_invite_age(self):
        self.code.has_warranty = False
        self.record.is_warranty_redemption = False
        self.code.used_at = self.record.redeemed_at = self.now - timedelta(days=60)
        await self.db.commit()
        result = await WarrantyService().scan_expired_warranty_codes(self.db)
        self.assertTrue(result['success'], result.get('error'))
        self.assertEqual(result['total'], 0)

    async def test_expired_warranty_stays_until_team_expiry(self):
        await self.joined('premium')
        self.code.warranty_expires_at = self.now - timedelta(days=1)
        await self.db.commit()
        self.assertEqual((await self.view())['status_label'], '质保已结束')
        self.assertEqual(await MemberAutoKickService()._due_members(self.db), [])

    async def test_team_expiry_schedules_warranty_even_with_pending_quota_action(self):
        from app.models import RotationAction
        await self.joined('premium')
        self.team.expires_at = self.now - timedelta(seconds=1)
        self.db.add(RotationAction(team_id=1, email=EMAIL, action_type='remove',
                                  status='pending', idempotency_key='stale'))
        await self.db.commit()
        delete = AsyncMock(return_value={'success': True})
        result = await MemberAutoKickService().run_due_members(self.db, delete)
        self.assertEqual(result['kicked'], 1)
        delete.assert_awaited_once()
        self.assertEqual((await self.view())['seat_status'], 'overdue')

    async def test_extension_cancels_old_queued_team_expiry(self):
        await self.joined()
        self.team.expires_at = self.now - timedelta(seconds=1)
        await self.db.commit()
        scanner = MemberAutoKickService()
        due = await scanner._due_members(self.db)
        self.team.expires_at = self.now + timedelta(days=30)
        await self.db.commit()
        scanner._due_members = AsyncMock(return_value=due)
        delete = AsyncMock()
        await scanner.run_due_members(self.db, delete)
        delete.assert_not_awaited()

    async def test_extension_during_member_login_cancels_self_exit(self):
        from types import SimpleNamespace
        await self.joined()
        self.team.expires_at = self.now - timedelta(seconds=1)
        await self.db.commit()
        remote = SimpleNamespace(delete_workspace_user=AsyncMock())
        departure = AccountPoolMemberExit(authorization=None, remote=remote)
        async def login(target):
            self.team.expires_at = self.now + timedelta(days=20)
            await self.db.commit()
            return {'access_token': 'token'}
        departure._login = login
        result = await departure(MemberExitTarget(self.team, 'user', EMAIL, self.db))
        self.assertFalse(result['success'])
        remote.delete_workspace_user.assert_not_awaited()

    async def test_sync_preserves_failure_retry_for_same_team_deadline(self):
        await self.joined()
        self.mapping.auto_kick_failures = 2
        self.mapping.auto_kick_last_error = 'remote failure'
        retry = self.now + timedelta(minutes=30)
        self.mapping.auto_kick_retry_at = retry
        await self.db.commit()
        await self.joined()
        self.assertEqual(self.mapping.auto_kick_failures, 2)
        self.assertEqual(self.mapping.auto_kick_retry_at, retry)

    async def test_premium_upgrade_restarts_usage_once_and_keeps_warranty_policy(self):
        await self.joined()
        self.mapping.usage_started_at = self.now - timedelta(hours=5)
        await self.db.commit()
        await self.joined('premium')
        upgraded_at = self.mapping.usage_started_at
        self.assertGreaterEqual(upgraded_at, self.now)
        self.assertEqual(self.mapping.auto_kick_at, self.team.expires_at)
        await self.joined('premium')
        self.assertEqual(self.mapping.usage_started_at, upgraded_at)

    async def test_normal_premium_upgrade_restarts_hourly_schedule(self):
        self.code.has_warranty = self.record.is_warranty_redemption = False
        await self.db.commit()
        await self.joined()
        self.mapping.usage_started_at = self.now - timedelta(hours=5)
        await self.db.commit()
        await self.joined('premium')
        self.assertEqual(self.mapping.auto_kick_at, self.mapping.usage_started_at + timedelta(hours=2))
        self.assertEqual((await self.view())['seat_status'], 'timed')

    async def test_owner_and_invites_are_not_kicked_when_team_expired(self):
        self.team.expires_at = self.now - timedelta(days=1)
        await self.db.commit()
        self.assertEqual(await MemberAutoKickService()._due_members(self.db), [])
        await self.joined()
        self.mapping.member_role = 'account-owner'
        await self.db.commit()
        self.assertEqual(await MemberAutoKickService()._due_members(self.db), [])
        self.assertEqual((await self.view())['seat_status'], 'owner')

    async def test_edit_team_changes_redemption_routing(self):
        self.assertFalse(self.team.warranty_seat_enabled)
        result = await TeamService().update_team(1, self.db, warranty_seat_enabled=True)
        self.assertTrue(result['success'], result.get('error'))
        self.assertTrue(self.team.warranty_seat_enabled)

    async def test_listing_keeps_activation_when_balance_is_unavailable(self):
        from types import SimpleNamespace
        teams = TeamService()
        teams.ensure_access_token = AsyncMock(return_value='unused')
        teams.chatgpt_service = SimpleNamespace(
            get_members=AsyncMock(return_value={'success': True, 'total': 1, 'members': [
                {'id': 'user', 'email': EMAIL, 'role': 'standard-user', 'seat_type': 'default',
                 'created_time': self.now.isoformat()}]}),
            get_invites=AsyncMock(return_value={'success': True, 'items': []}))
        teams.get_team_seat_balance = AsyncMock(return_value={'success': False, 'error': 'unavailable'})
        result = await teams.get_team_members(1, self.db)
        self.assertTrue(result['success'], result.get('error'))
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as verify:
            record = await verify.get(RedemptionRecord, self.record.id)
            self.assertFalse(record.activation_pending)
            self.assertEqual(record.service_started_at, self.now)

    async def test_changed_team_expiry_does_not_inherit_stale_retry(self):
        await self.joined()
        self.mapping.auto_kick_failures = 3
        self.mapping.auto_kick_retry_at = self.now + timedelta(hours=2)
        self.team.expires_at = self.now - timedelta(seconds=1)
        await self.db.commit()
        due = await MemberAutoKickService()._due_members(self.db)
        self.assertEqual(len(due), 1)
        self.assertIsNone(self.mapping.auto_kick_retry_at)
        self.assertEqual(self.mapping.auto_kick_at, self.team.expires_at)

    async def test_legacy_binding_alone_also_has_warranty_preview(self):
        await self.db.delete(self.record)
        self.code.warranty_expires_at = self.now + timedelta(days=10)
        await self.db.commit()
        await self.joined()
        member = await self.view()
        self.assertEqual(member['seat_status'], 'warranty')
        self.assertEqual(member['warranty_expires_at'], self.code.warranty_expires_at.isoformat())
