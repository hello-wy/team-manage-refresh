"""席位类型决定下线周期，覆盖历史计划、续费和质保升级限制。"""
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import QuotaSnapshot, RedemptionCode, RotationAction, Team, TeamEmailMapping
from app.services.member_auto_kick import MemberAutoKickService, schedule_payload
from app.services.member_departure import AccountPoolMemberExit, MemberExitTarget
from app.services.member_invitation import InviteRequest, MemberInvitationService
from app.services.member_seat_status import decorate_seat_status
from app.services.quota_rotation import load_rotation_data, reconcile_action, _execute_remote
from app.services.quota_rotation_policy import next_action
from app.services.team import TeamService
from app.services.warranty import WarrantyService
from app.utils.time_utils import get_now

EMAIL = 'member@example.com'
USER = 'user-member'


class SeatDeparturePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.now = get_now()
        self.team = Team(id=1, email='owner@example.com', account_id='space',
                         access_token_encrypted='unused', member_auto_kick_hours=2,
                         expires_at=self.now + timedelta(days=20), rotation_mode='auto')
        self.mapping = TeamEmailMapping(team_id=1, email=EMAIL, status='invited',
                                        last_invited_at=self.now - timedelta(minutes=5))
        self.db.add_all([self.team, self.mapping])
        await self.db.commit()
        self.teams = TeamService()
        self.scanner = MemberAutoKickService()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def sync(self, seat='standard', at=None):
        with patch('app.services.team.get_now', return_value=at or self.now):
            await self.teams._reconcile_team_email_mappings(1, {EMAIL}, set(), self.db,
                joined_members={EMAIL: {'id': USER, 'email': EMAIL, 'role': 'standard-user',
                                       'seat_type': seat, 'created_time': self.now.isoformat()}})
        await self.db.commit()

    async def warranty(self):
        self.db.add(RedemptionCode(code='WARRANTY', has_warranty=True, status='used',
                                   used_team_id=1, used_by_email=EMAIL, used_at=self.now))
        await self.db.commit()

    async def test_standard_keeps_team_expiry_when_hours_change_and_has_no_fake_warranty(self):
        await self.sync()
        self.assertEqual(self.mapping.departure_policy, 'standard_expiry')
        self.assertEqual(self.mapping.auto_kick_at, self.team.expires_at)
        await self.scanner.update_team_hours(1, 1, self.db)
        self.assertEqual(self.mapping.auto_kick_at, self.team.expires_at)
        self.assertFalse(schedule_payload(self.mapping)['auto_kick_enabled'])
        toggle = await self.scanner.update_member_participation(1, USER, True, self.db)
        self.assertFalse(toggle['success'])
        member = dict(email=EMAIL, role='standard-user', status='joined', seat_type='standard',
                      **schedule_payload(self.mapping))
        view, = await decorate_seat_status(self.db, self.team, [member])
        self.assertEqual(view['status_label'], '保留至 Team 到期')
        self.assertFalse(view['warranty_protected'])
        self.assertEqual(await self.scanner._due_members(self.db), [])

    async def test_premium_starts_after_acceptance_and_upgrade_restarts_once(self):
        self.assertIsNone(self.mapping.auto_kick_at)
        self.assertEqual(await self.scanner._due_members(self.db), [])
        await self.sync('premium')
        self.assertEqual(self.mapping.auto_kick_at, self.now + timedelta(hours=2))
        await self.sync('premium', self.now + timedelta(minutes=20))
        self.assertEqual(self.mapping.auto_kick_at, self.now + timedelta(hours=2))
        await self.sync('standard', self.now + timedelta(minutes=30))
        self.assertEqual(self.mapping.auto_kick_at, self.team.expires_at)
        upgraded_at = self.now + timedelta(hours=1)
        await self.sync('premium', upgraded_at)
        self.assertEqual(self.mapping.auto_kick_at, upgraded_at + timedelta(hours=2))
        await self.sync('premium', upgraded_at + timedelta(minutes=10))
        self.assertEqual(self.mapping.auto_kick_at, upgraded_at + timedelta(hours=2))

    async def test_stale_hourly_schedule_never_kicks_standard_before_team_expiry(self):
        self.mapping.status = 'joined'
        self.mapping.seat_type = 'standard'
        self.mapping.upstream_user_id = USER
        self.mapping.auto_kick_at = self.now - timedelta(hours=1)
        self.mapping.joined_at = self.now - timedelta(hours=3)
        await self.db.commit()
        delete = AsyncMock()
        self.assertEqual((await self.scanner.run_due_members(self.db, delete))['scanned'], 0)
        delete.assert_not_awaited()
        departure = AccountPoolMemberExit(authorization=None, remote=None)
        departure._login = AsyncMock()
        result = await departure(MemberExitTarget(self.team, USER, EMAIL, self.db))
        self.assertFalse(result['success'])
        departure._login.assert_not_awaited()
        self.assertEqual(self.mapping.auto_kick_at, self.team.expires_at)

    async def test_missing_team_expiry_never_falls_back_to_hourly_standard_departure(self):
        self.team.expires_at = None
        await self.sync()
        self.assertIsNone(self.mapping.auto_kick_at)
        self.assertEqual(await self.scanner._due_members(self.db), [])
        member = dict(email=EMAIL, role='standard-user', status='joined', seat_type='standard',
                      **schedule_payload(self.mapping))
        view, = await decorate_seat_status(self.db, self.team, [member])
        self.assertIn('到期时间待同步', view['status_reason'])

    async def test_standard_team_expiry_overrides_old_opt_out_and_old_retry_deadline(self):
        await self.sync()
        self.mapping.auto_kick_enabled = False
        self.mapping.auto_kick_retry_at = self.now + timedelta(hours=2)
        self.mapping.auto_kick_failures = 3
        self.team.expires_at = self.now - timedelta(seconds=1)
        await self.db.commit()
        delete = AsyncMock(return_value={'success': True})
        result = await self.scanner.run_due_members(self.db, delete)
        self.assertEqual(result['kicked'], 1)
        delete.assert_awaited_once()
        self.assertEqual(self.mapping.auto_kick_at, self.team.expires_at)
        self.assertIsNone(self.mapping.auto_kick_retry_at)

    async def test_standard_renewal_during_login_cancels_old_expiry(self):
        self.team.expires_at = self.now - timedelta(seconds=1)
        await self.sync()
        remote = SimpleNamespace(delete_workspace_user=AsyncMock())
        departure = AccountPoolMemberExit(authorization=None, remote=remote)
        async def login(target):
            self.team.expires_at = self.now + timedelta(days=30)
            await self.db.commit()
            return {'access_token': 'token'}
        departure._login = login
        result = await departure(MemberExitTarget(self.team, USER, EMAIL, self.db))
        self.assertFalse(result['success'])
        remote.delete_workspace_user.assert_not_awaited()
        self.assertEqual(self.mapping.auto_kick_at, self.team.expires_at)

    async def test_warranty_blocks_manual_upgrade_and_premium_invite(self):
        await self.warranty()
        await self.sync()
        self.teams.ensure_access_token = AsyncMock(return_value='token')
        self.teams.chatgpt_service = SimpleNamespace(get_members=AsyncMock(return_value={
            'success': True, 'members': [{'id': USER, 'email': EMAIL, 'seat_type': 'standard'}],
        }), update_member_seat_type=AsyncMock())
        result = await self.teams.update_member_seat_type(1, USER, 'premium', 'standard', self.db)
        self.assertEqual(result['error_code'], 'standard_seat_locked')
        self.teams.chatgpt_service.update_member_seat_type.assert_not_awaited()
        result = await MemberInvitationService(self.teams, self.db).invite(
            InviteRequest(team_id=1, email=EMAIL, seat_type='premium'))
        self.assertEqual(result['error_code'], 'warranty_standard_only')

    async def test_quota_rotation_remote_actions_cannot_mutate_standard_seats(self):
        await self.sync('standard')
        action = RotationAction(team_id=1, email=EMAIL, action_type='remove',
                                idempotency_key='standard-remove')
        remote = SimpleNamespace(delete_team_member=AsyncMock(), update_member_seat_type=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, '仅允许处理高级席位'):
            await _execute_remote(self.db, self.team, action, mapping=self.mapping, team_service=remote)
        remote.delete_team_member.assert_not_awaited()
        remote.update_member_seat_type.assert_not_awaited()

    async def test_warranty_blocks_auto_upgrade_and_cancels_old_action(self):
        await self.warranty()
        await self.sync()
        self.db.add(QuotaSnapshot(email=EMAIL, team_space_id='space', observed_seat_type='standard',
                                  status='ok', weekly_remaining=0, observed_at=get_now()))
        action = RotationAction(team_id=1, email=EMAIL, action_type='upgrade', status='pending',
                                idempotency_key='old-upgrade')
        self.db.add(action)
        await self.db.commit()
        mappings, snapshots, states = await load_rotation_data(self.db, self.team)
        seats = {'success': True, 'balance': {
            'standard': {'known': True, 'remaining': 0}, 'premium': {'known': True, 'remaining': 1}}}
        self.assertIsNone(next_action(mappings, snapshots, states, seats))
        remote = SimpleNamespace(get_team_members=AsyncMock(), update_member_seat_type=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, '不能升级'):
            await _execute_remote(self.db, self.team, action, mapping=self.mapping, team_service=remote)
        self.assertEqual(await reconcile_action(self.db, action, remote), 'cancelled')
        remote.update_member_seat_type.assert_not_awaited()
        remote.get_team_members.assert_not_awaited()

    async def test_submitted_upstream_upgrade_is_reconciled_without_resubmitting(self):
        await self.sync()
        action = RotationAction(team_id=1, email=EMAIL, action_type='upgrade', status='executing',
                                idempotency_key='upstream-pending-upgrade')
        self.db.add(action)
        await self.db.commit()
        remote = SimpleNamespace(get_team_members=AsyncMock(return_value={
            'success': True, 'members': [{'email': EMAIL, 'seat_type': 'standard', 'pending_seat_type': 'prolite'}],
        }), update_member_seat_type=AsyncMock())
        self.assertEqual(await reconcile_action(self.db, action, remote), 'reconciling')
        remote.update_member_seat_type.assert_not_awaited()
        remote.get_team_members.assert_awaited_once()

    async def test_old_day_based_jobs_skip_standard_members(self):
        await self.sync()
        self.mapping.joined_at = self.now - timedelta(days=60)
        self.db.add(RedemptionCode(code='NORMAL', has_warranty=False, status='used',
                                   used_team_id=1, used_by_email=EMAIL, used_at=self.now - timedelta(days=60)))
        await self.db.commit()
        service = WarrantyService()
        result = await service.kick_and_destroy_expired_warranty_code(self.db, 'NORMAL')
        self.assertEqual(result.get('skip_reason'), 'standard_seat')
        # No redemption is required for the unauthorized-member path.
        result = await service.kick_unauthorized_team_member(self.db, 1, EMAIL)
        self.assertEqual(result.get('skip_reason'), 'standard_seat')
        self.mapping.is_admin_invited = True
        await self.db.commit()
        result = await service.kick_admin_invited_expired_member(self.db, 1, EMAIL)
        self.assertEqual(result.get('skip_reason'), 'standard_seat')
        self.assertEqual(self.mapping.status, 'joined')

    async def test_standard_display_ignores_missing_or_legacy_policy_and_opt_out(self):
        for seat in ('default', 'standard'):
            member = dict(email=EMAIL, role='standard-user', status='joined', seat_type=seat,
                          departure_policy='timed', auto_kick_enabled=False,
                          auto_kick_at=(self.now - timedelta(hours=1)).isoformat())
            view, = await decorate_seat_status(self.db, self.team, [member])
            self.assertEqual(view['auto_kick_at'], self.team.expires_at.isoformat())
            self.assertEqual(view['departure_policy'], 'standard_expiry')
            self.assertEqual(view['status_label'], '保留至 Team 到期')

    async def test_standard_blocks_auto_upgrade_and_cancels_unapplied_old_action(self):
        await self.sync()
        action = RotationAction(team_id=1, email=EMAIL, action_type='upgrade', status='pending',
                                idempotency_key='old-standard-upgrade')
        self.db.add(action)
        await self.db.commit()
        remote = SimpleNamespace(get_team_members=AsyncMock(return_value={
            'success': True, 'members': [{'email': EMAIL, 'seat_type': 'standard'}],
        }), update_member_seat_type=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, '不能升级'):
            await _execute_remote(self.db, self.team, action, mapping=self.mapping, team_service=remote)
        self.assertEqual(await reconcile_action(self.db, action, remote), 'cancelled')
        remote.update_member_seat_type.assert_not_awaited()
