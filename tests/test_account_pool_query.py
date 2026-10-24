import unittest
import json
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, RotationAction, Team, TeamEmailMapping
from app.services.account_pool import account_pool_service
from app.services.account_pool_rotation import ROTATION_FILTER_OPTIONS, attach_rotation_status
from app.utils.time_utils import get_now


class AccountPoolQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = get_now()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add_all([Team(id=i, email=f'owner{i}@example.com', team_name='Same Name',
                             access_token_encrypted='unused', account_id=f'space-{i}',
                             expires_at=self.now + timedelta(days=20)) for i in (1, 2)])
            for i, name in enumerate(['standard', 'premium', 'personal', 'conflict', 'owner1', 'invited']):
                db.add(AccountPoolEntry(email=f'{name}@example.com', created_at=self.now + timedelta(seconds=i),
                                         updated_at=self.now + timedelta(seconds=i), workspace_status='personal_account'))
            db.add_all([
                TeamEmailMapping(team_id=1, email='standard@example.com', status='joined', seat_type='standard',
                                 standard_auto_kick_enabled=True,
                                 auto_kick_at=self.now + timedelta(hours=1)),
                TeamEmailMapping(team_id=1, email='premium@example.com', status='joined', seat_type='premium',
                                 auto_kick_at=self.now + timedelta(hours=3)),
                TeamEmailMapping(team_id=1, email='owner1@example.com', status='joined', seat_type='standard',
                                 auto_kick_at=self.now - timedelta(days=1)),
                TeamEmailMapping(team_id=1, email='conflict@example.com', status='joined', seat_type='standard',
                                 standard_auto_kick_enabled=True,
                                 auto_kick_at=self.now + timedelta(hours=2)),
                TeamEmailMapping(team_id=2, email='conflict@example.com', status='joined', seat_type='premium'),
                TeamEmailMapping(team_id=2, email='invited@example.com', status='invited'),
            ])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def listing(self, **filters):
        async with self.sessions() as db:
            return await account_pool_service.list_entries(db, **filters)

    def emails(self, listing):
        return [row['email'] for row in listing['entries']]

    async def test_default_group_order_is_stable_across_pages_and_keeps_owners_unique(self):
        whole = await self.listing()
        self.assertEqual(whole['total'], 7)
        emails = self.emails(whole)
        self.assertEqual(emails[:5], ['owner1@example.com', 'premium@example.com', 'conflict@example.com', 'standard@example.com', 'owner2@example.com'])
        self.assertLess(emails.index('premium@example.com'), emails.index('standard@example.com'))
        self.assertLess(emails.index('owner2@example.com'), emails.index('personal@example.com'))
        async with self.sessions() as db:
            entries = (await db.execute(select(AccountPoolEntry))).scalars().all()
            for index, entry in enumerate(reversed(entries)):
                entry.updated_at = self.now + timedelta(days=index + 1)
            await db.commit()
        self.assertEqual(self.emails(await self.listing()), emails)
        pages = [await self.listing(page=i, per_page=2) for i in range(1, 5)]
        self.assertEqual([email for page in pages for email in self.emails(page)], emails)
        self.assertEqual((await self.listing(page=999, per_page=2))['current_page'], 4)

    async def test_team_and_seat_must_match_same_membership(self):
        self.assertEqual(set(self.emails(await self.listing(team_filter='1', seat_filter='premium'))),
                         {'premium@example.com'})
        self.assertEqual(set(self.emails(await self.listing(team_filter='2', seat_filter='premium'))),
                         {'conflict@example.com'})
        self.assertIn('owner1@example.com', self.emails(await self.listing(team_filter='1')))
        self.assertIn('owner2@example.com', self.emails(await self.listing(team_filter='2')))
        self.assertEqual(self.emails(await self.listing(team_filter='999')), [])

    async def test_personal_and_legacy_status_filters_use_actual_memberships(self):
        personal = {'personal@example.com', 'invited@example.com'}
        self.assertEqual(set(self.emails(await self.listing(team_filter='personal'))), personal)
        self.assertEqual(set(self.emails(await self.listing(status_filter='not_joined'))), personal)
        self.assertIn('standard@example.com', self.emails(await self.listing(status_filter='joined')))
        self.assertEqual(self.emails(await self.listing(team_filter='personal', seat_filter='premium')), [])
        self.assertEqual(self.emails(await self.listing(status_filter='conflict')), ['conflict@example.com'])
        self.assertEqual(self.emails(await self.listing(status_filter='invited')), ['invited@example.com'])

    async def test_sort_choices_and_filtered_total_are_global(self):
        recent = await self.listing(sort_by='newest', search='example.com', per_page=2)
        self.assertEqual(self.emails(recent), ['invited@example.com', 'owner1@example.com'])
        self.assertEqual(recent['total'], 7)
        deadline = self.emails(await self.listing(sort_by='deadline'))
        self.assertEqual(deadline[:3], ['premium@example.com', 'conflict@example.com', 'standard@example.com'])
        alpha = self.emails(await self.listing(sort_by='email'))
        self.assertEqual(alpha, sorted(alpha))
        self.assertEqual(self.emails(await self.listing(team_filter='1', seat_filter='standard', search='STANDARD')),
                         ['standard@example.com'])

    async def test_header_sort_direction_applies_before_pagination_and_keeps_unknowns_last(self):
        whole = self.emails(await self.listing(sort_by='email', sort_order='desc'))
        self.assertEqual(whole, sorted(whole, reverse=True))
        pages = [await self.listing(sort_by='email', sort_order='desc', page=i, per_page=2)
                 for i in range(1, 5)]
        self.assertEqual([email for page in pages for email in self.emails(page)], whole)
        descending = await self.listing(sort_by='team', sort_order='desc')
        self.assertEqual(self.emails(descending)[0], 'owner2@example.com')
        self.assertEqual(set(self.emails(descending)[-2:]), {'personal@example.com', 'invited@example.com'})
        self.assertEqual((await self.listing(sort_order='invalid'))['sort_order'], 'asc')

    async def test_name_and_liveness_header_sort_use_current_owner_results(self):
        async with self.sessions() as db:
            entries = {entry.email: entry for entry in (await db.scalars(select(AccountPoolEntry))).all()}
            entries['standard@example.com'].name = 'Zoe'
            entries['premium@example.com'].name = 'Alice'
            entries['standard@example.com'].liveness_status = 'alive'
            entries['standard@example.com'].liveness_checked_at = self.now
            team = await db.get(Team, 2)
            team.owner_liveness_status = 'invalid'
            team.owner_liveness_checked_at = self.now
            await db.commit()
        self.assertEqual(self.emails(await self.listing(sort_by='name'))[:2],
                         ['premium@example.com', 'standard@example.com'])
        self.assertEqual(self.emails(await self.listing(sort_by='name', sort_order='desc'))[:2],
                         ['standard@example.com', 'premium@example.com'])
        self.assertEqual(self.emails(await self.listing(sort_by='liveness'))[:2],
                         ['standard@example.com', 'owner2@example.com'])
        self.assertEqual(self.emails(await self.listing(sort_by='liveness', sort_order='desc'))[:2],
                         ['owner2@example.com', 'standard@example.com'])

    async def test_deadline_sort_uses_plans_including_quota_rotation_teams(self):
        from sqlalchemy import select
        async with self.sessions() as db:
            mapping = (await db.execute(select(TeamEmailMapping).where(
                TeamEmailMapping.email == 'standard@example.com'))).scalar_one()
            mapping.auto_kick_at = None
            await db.commit()
            listing = await account_pool_service.list_entries(db, sort_by='deadline')
            self.assertEqual(self.emails(listing)[:2], ['premium@example.com', 'conflict@example.com'])
            team = await db.get(Team, 1)
            team.rotation_mode = 'auto'
            await db.commit()
            listing = await account_pool_service.list_entries(db, sort_by='deadline')
            # Member schedules still run on quota-rotation Teams, so their deadlines keep sorting first.
            self.assertEqual(self.emails(listing)[:2], ['premium@example.com', 'conflict@example.com'])

    async def test_deadline_sort_uses_team_expiry_for_standard_seats(self):
        from sqlalchemy import select
        async with self.sessions() as db:
            mappings = (await db.scalars(select(TeamEmailMapping).where(
                TeamEmailMapping.seat_type == 'standard'))).all()
            for mapping in mappings:
                mapping.standard_auto_kick_enabled = False
            await db.commit()
            listing = await account_pool_service.list_entries(db, sort_by='deadline')
            self.assertEqual(self.emails(listing)[0], 'premium@example.com')

    async def test_normalizes_invalid_options_and_loads_details_for_one_page_only(self):
        async with self.sessions() as db:
            with patch.object(account_pool_service, 'rows_by_ids', wraps=account_pool_service.rows_by_ids) as load:
                listing = await account_pool_service.list_entries(db, per_page=2, team_filter='9' * 200,
                                                                 seat_filter='invalid', sort_by='invalid',
                                                                 rotation_filter='invalid', liveness_filter='invalid')
            self.assertEqual(len(load.call_args.args[1]), 2)
            self.assertEqual(listing['team_filter'], '')
            self.assertEqual(listing['seat_filter'], '')
            self.assertEqual(listing['sort_by'], 'team')
            self.assertEqual(listing['rotation_filter'], '')
            self.assertEqual(listing['liveness_filter'], '')

    async def test_rotation_filter_matches_display_policy_for_every_option(self):
        async with self.sessions() as db:
            whole = await account_pool_service.list_entries(db)
            rows = await attach_rotation_status(db, whole['entries'])
        self.assertEqual(set(ROTATION_FILTER_OPTIONS), {'invited', 'waiting_exit', 'idle', 'excluded'})
        for key in ROTATION_FILTER_OPTIONS:
            with self.subTest(status=key):
                filtered = await self.listing(rotation_filter=key)
                expected = [row['email'] for row in rows if row['rotation']['status'] == key]
                self.assertEqual(self.emails(filtered), expected)
                self.assertEqual(filtered['total'], len(expected))
                self.assertTrue(all(row['rotation']['status'] == key for row in filtered['entries']))

    async def test_rotation_filter_paginates_matches_globally_and_loads_one_page(self):
        # Both owners, including the virtual owner, take priority over their mappings.
        async with self.sessions() as db:
            with patch.object(account_pool_service, 'rows_by_ids', wraps=account_pool_service.rows_by_ids) as load:
                first = await account_pool_service.list_entries(db, rotation_filter='excluded', per_page=1)
            self.assertEqual(len(load.call_args.args[1]), 1)
        second = await self.listing(rotation_filter='excluded', per_page=1, page=999)
        self.assertEqual(first['total'], 2)
        self.assertEqual(first['total_pages'], 2)
        self.assertEqual(self.emails(first), ['owner1@example.com'])
        self.assertEqual(self.emails(second), ['owner2@example.com'])
        self.assertEqual(second['current_page'], 2)

    async def test_rotation_filter_combines_with_existing_filters_and_sort(self):
        invited = await self.listing(rotation_filter='invited', team_filter='personal',
                                     seat_filter='unknown', search='INVITED', sort_by='newest')
        self.assertEqual(self.emails(invited), ['invited@example.com'])
        self.assertEqual(invited['entries'][0]['rotation']['label'], '待接受邀请')
        self.assertEqual(self.emails(await self.listing(rotation_filter='waiting_exit',
                                                       team_filter='1', seat_filter='standard')),
                         ['standard@example.com'])
        empty = await self.listing(rotation_filter='invited', seat_filter='premium', page=999)
        self.assertEqual((empty['total'], empty['total_pages'], empty['current_page']), (0, 1, 1))
        self.assertEqual(empty['entries'], [])

    async def test_rotation_filter_respects_action_and_conflict_priority(self):
        async with self.sessions() as db:
            db.add_all([
                RotationAction(team_id=2, email='invited@example.com', action_type='invite',
                               status='pending', idempotency_key='invite'),
                RotationAction(team_id=1, email='conflict@example.com', action_type='remove',
                               status='reconciling', idempotency_key='remove'),
                RotationAction(team_id=1, email='owner1@example.com', action_type='remove',
                               status='executing', idempotency_key='owner'),
            ])
            await db.commit()
        self.assertEqual(self.emails(await self.listing(rotation_filter='invited')), [])
        rows = await self.listing(sort_by='email')
        async with self.sessions() as db:
            await attach_rotation_status(db, rows['entries'])
        by_email = {row['email']: row['rotation'] for row in rows['entries']}
        self.assertEqual(by_email['conflict@example.com']['detail'], '退出确认中')
        self.assertEqual(by_email['invited@example.com']['detail'], '邀请确认中')
        for email in ('conflict@example.com', 'invited@example.com'):
            self.assertEqual(by_email[email]['label'], '')
            self.assertEqual(by_email[email]['status'], '')
        for removed in ('processing', 'attention', 'using', 'waiting_rotation'):
            self.assertNotIn(removed, ROTATION_FILTER_OPTIONS)
            self.assertEqual((await self.listing(rotation_filter=removed))['rotation_filter'], '')
        self.assertEqual(self.emails(await self.listing(rotation_filter='excluded')),
                         ['owner1@example.com', 'owner2@example.com'])

    async def test_page_renders_selected_rotation_and_preserves_it_in_links(self):
        from starlette.requests import Request
        from app.routes.admin import account_pool_page
        request = Request({'type': 'http', 'method': 'GET', 'path': '/admin/account-pool',
                           'query_string': b'rotation_filter=excluded', 'headers': []})
        async with self.sessions() as db:
            response = await account_pool_page(request, db=db, current_user={'username': 'admin'},
                                               rotation_filter='excluded', per_page=1)
        html = response.body.decode()
        self.assertIn('value="excluded" selected', html)
        self.assertIn('rotation_filter=excluded', response.context['filter_url'](page=2))
        self.assertNotIn('rotation_filter=excluded', response.context['filter_url'](rotation_filter=''))
        self.assertEqual(response.context['total'], 2)

    async def test_liveness_filters_apply_before_pagination_and_combine_with_rotation(self):
        async with self.sessions() as db:
            entries = (await db.scalars(select(AccountPoolEntry).order_by(AccountPoolEntry.id))).all()
            statuses = ['alive', 'invalid', None, 'error', '', 'missing']
            for entry, status in zip(entries, statuses):
                entry.liveness_status = status
                entry.liveness_checked_at = self.now if status else None
            await db.commit()
        expected = {'alive': ['standard@example.com'],
                    'abnormal': ['conflict@example.com', 'invited@example.com', 'premium@example.com'],
                    'unchecked': ['owner1@example.com', 'owner2@example.com', 'personal@example.com']}
        for status, emails in expected.items():
            first = await self.listing(liveness_filter=status, sort_by='email', per_page=1)
            self.assertEqual(first['total'], len(emails))
            pages = [await self.listing(liveness_filter=status, sort_by='email', per_page=1, page=i)
                     for i in range(1, len(emails) + 1)]
            self.assertEqual([row['email'] for page in pages for row in page['entries']], emails)
        combined = await self.listing(liveness_filter='abnormal', rotation_filter='invited',
                                      team_filter='personal', seat_filter='unknown', search='INVITED')
        self.assertEqual(self.emails(combined), ['invited@example.com'])
        self.assertEqual((await self.listing(liveness_filter='alive', seat_filter='premium'))['total'], 0)

    async def test_liveness_filter_matches_latest_owner_result_and_timestamp_ties(self):
        async with self.sessions() as db:
            owner1 = await db.get(Team, 1)
            owner1.owner_liveness_status = 'invalid'
            owner1.owner_liveness_checked_at = self.now
            owner2 = await db.get(Team, 2)
            owner2.owner_liveness_status = 'error'
            owner2.owner_liveness_checked_at = self.now
            db.add(Team(id=3, email=' OWNER2@EXAMPLE.COM ', account_id='space-3',
                        access_token_encrypted='unused', owner_liveness_status='alive',
                        owner_liveness_checked_at=self.now + timedelta(seconds=1)))
            entry = await db.scalar(select(AccountPoolEntry).where(AccountPoolEntry.email == owner1.email))
            entry.liveness_status = 'alive'
            entry.liveness_checked_at = self.now
            await db.commit()
        # Virtual owner uses the newest Team, real owner prefers pool on a tie.
        self.assertEqual(self.emails(await self.listing(liveness_filter='alive', sort_by='email')),
                         ['owner1@example.com', 'owner2@example.com'])
        async with self.sessions() as db:
            owner1 = await db.get(Team, 1)
            owner1.owner_liveness_checked_at = self.now + timedelta(seconds=2)
            await db.commit()
        self.assertEqual(self.emails(await self.listing(liveness_filter='alive')), ['owner2@example.com'])
        abnormal = await self.listing(liveness_filter='abnormal')
        self.assertEqual(self.emails(abnormal), ['owner1@example.com'])
        self.assertEqual(abnormal['entries'][0]['liveness_status'], 'invalid')
        self.assertEqual((await self.listing(liveness_filter='unchecked'))['total'], 5)

    async def test_page_hides_saved_success_message_and_preserves_liveness_filters(self):
        from starlette.requests import Request
        from app.routes.admin import account_pool_page
        message = '密码、2FA 与 OAuth 登录验证通过'
        async with self.sessions() as db:
            entries = (await db.scalars(select(AccountPoolEntry))).all()
            for entry in entries:
                entry.liveness_status = 'alive'
                entry.liveness_message = message
                entry.liveness_checked_at = self.now
            await db.commit()
            request = Request({'type': 'http', 'method': 'GET', 'path': '/admin/account-pool',
                               'query_string': b'liveness_filter=alive', 'headers': []})
            response = await account_pool_page(request, db=db, current_user={'username': 'admin'},
                                               liveness_filter='alive', per_page=1)
        html = response.body.decode()
        self.assertNotIn(message, html)
        self.assertIn('class="account-pool-liveness-message" hidden', html)
        self.assertIn('value="alive" selected', html)
        self.assertIn('id="accountPoolBatchLiveness"', html)
        self.assertIn('liveness_filter=alive', response.context['filter_url'](page=2, seat_filter='standard'))
        self.assertNotIn('liveness_filter=alive', response.context['filter_url'](liveness_filter=''))
        for label in ('需关注', '处理中', '使用中'):
            self.assertNotIn('>' + label + '<', html)

    async def test_header_controls_replace_sort_dropdown_and_keep_direction_in_links(self):
        from starlette.requests import Request
        from app.routes.admin import account_pool_page
        request = Request({'type': 'http', 'method': 'GET', 'path': '/admin/account-pool',
                           'query_string': b'', 'headers': []})
        async with self.sessions() as db:
            response = await account_pool_page(request, db=db, current_user={'username': 'admin'},
                                               sort_by='email', sort_order='desc')
        html = response.body.decode()
        self.assertNotIn('id="accountPoolSort"', html)
        self.assertIn('id="accountPoolCheckAllLiveness"', html)
        self.assertIn('aria-sort="descending"', html)
        self.assertIn('data-sort-by="email"', html)
        self.assertIn('name="sort_order" value="desc"', html)
        self.assertIn('sort_order=desc', response.context['filter_url'](page=2))

    async def test_liveness_targets_include_all_pages_and_unique_owners_without_loading_details(self):
        from app.routes.admin import account_pool_liveness_targets
        async with self.sessions() as db:
            db.add_all([AccountPoolEntry(email=f'bulk{i:03}@example.com') for i in range(105)])
            db.add(Team(id=3, email=' OWNER2@EXAMPLE.COM ', account_id='space-3',
                        access_token_encrypted='unused'))
            db.add(AccountPoolEntry(email='deleted@example.com', deleted_at=self.now))
            await db.commit()
            with patch.object(account_pool_service, 'rows_by_ids', side_effect=AssertionError('Must not load credentials')):
                response = await account_pool_liveness_targets(db=db, current_user={'username': 'admin'})
        result = json.loads(response.body)
        self.assertEqual(set(result), {'success', 'ids', 'total'})
        self.assertEqual(result['total'], 112)
        self.assertEqual(len(result['ids']), 112)
        self.assertEqual(len(set(result['ids'])), 112)
        self.assertTrue(all(entry_id > 0 for entry_id in result['ids']))
        async with self.sessions() as db:
            owner2_id = await db.scalar(select(AccountPoolEntry.id).where(
                AccountPoolEntry.email == 'owner2@example.com',
            ))
        self.assertIn(owner2_id, result['ids'])
        self.assertNotIn(-3, result['ids'])
        self.assertEqual(response.headers['cache-control'], 'no-store')

    async def test_liveness_targets_match_combined_list_filters(self):
        from app.routes.admin import account_pool_liveness_targets
        filters = dict(team_filter='personal', seat_filter='unknown', rotation_filter='invited',
                       liveness_filter='unchecked', search='INVITED')
        expected = await self.listing(**filters)
        async with self.sessions() as db:
            response = await account_pool_liveness_targets(db=db, current_user={'username': 'admin'}, **filters)
        result = json.loads(response.body)
        self.assertEqual(result['ids'], [row['id'] for row in expected['entries']])
        self.assertEqual(result['total'], 1)
