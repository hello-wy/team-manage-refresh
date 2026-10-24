import unittest
from datetime import timedelta
from unittest.mock import patch

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
                             access_token_encrypted='unused', account_id=f'space-{i}') for i in (1, 2)])
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
        self.assertEqual(emails[:5], ['owner1@example.com', 'premium@example.com', 'standard@example.com', 'conflict@example.com', 'owner2@example.com'])
        self.assertLess(emails.index('premium@example.com'), emails.index('standard@example.com'))
        self.assertLess(emails.index('owner2@example.com'), emails.index('personal@example.com'))
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
        self.assertEqual(deadline[:3], ['standard@example.com', 'conflict@example.com', 'premium@example.com'])
        alpha = self.emails(await self.listing(sort_by='email'))
        self.assertEqual(alpha, sorted(alpha))
        self.assertEqual(self.emails(await self.listing(team_filter='1', seat_filter='standard', search='STANDARD')),
                         ['standard@example.com'])

    async def test_deadline_sort_uses_plans_including_quota_rotation_teams(self):
        from sqlalchemy import select
        async with self.sessions() as db:
            mapping = (await db.execute(select(TeamEmailMapping).where(
                TeamEmailMapping.email == 'standard@example.com'))).scalar_one()
            mapping.auto_kick_at = None
            await db.commit()
            listing = await account_pool_service.list_entries(db, sort_by='deadline')
            self.assertEqual(self.emails(listing)[:2], ['conflict@example.com', 'premium@example.com'])
            team = await db.get(Team, 1)
            team.rotation_mode = 'auto'
            await db.commit()
            listing = await account_pool_service.list_entries(db, sort_by='deadline')
            # Member schedules still run on quota-rotation Teams, so their deadlines keep sorting first.
            self.assertEqual(self.emails(listing)[:2], ['conflict@example.com', 'premium@example.com'])

    async def test_deadline_sort_uses_explicit_plan_for_every_seat_type(self):
        from sqlalchemy import select
        async with self.sessions() as db:
            mappings = (await db.scalars(select(TeamEmailMapping).where(
                TeamEmailMapping.seat_type == 'standard'))).all()
            for mapping in mappings:
                mapping.standard_auto_kick_enabled = False
            await db.commit()
            listing = await account_pool_service.list_entries(db, sort_by='deadline')
            self.assertEqual(self.emails(listing)[0], 'standard@example.com')

    async def test_normalizes_invalid_options_and_loads_details_for_one_page_only(self):
        async with self.sessions() as db:
            with patch.object(account_pool_service, 'rows_by_ids', wraps=account_pool_service.rows_by_ids) as load:
                listing = await account_pool_service.list_entries(db, per_page=2, team_filter='9' * 200,
                                                                 seat_filter='invalid', sort_by='invalid',
                                                                 rotation_filter='invalid')
            self.assertEqual(len(load.call_args.args[1]), 2)
            self.assertEqual(listing['team_filter'], '')
            self.assertEqual(listing['seat_filter'], '')
            self.assertEqual(listing['sort_by'], 'team')
            self.assertEqual(listing['rotation_filter'], '')

    async def test_rotation_filter_matches_display_policy_for_every_option(self):
        async with self.sessions() as db:
            whole = await account_pool_service.list_entries(db)
            rows = await attach_rotation_status(db, whole['entries'])
        self.assertEqual(len(ROTATION_FILTER_OPTIONS), 8)
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
        processing = await self.listing(rotation_filter='processing', sort_by='email')
        self.assertEqual(self.emails(processing), ['conflict@example.com', 'invited@example.com'])
        self.assertEqual([row['rotation']['detail'] for row in processing['entries']],
                         ['退出确认中', '邀请确认中'])
        self.assertEqual(self.emails(await self.listing(rotation_filter='attention')), [])
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
