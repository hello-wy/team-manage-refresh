import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, Team
from app.services.account_pool import account_pool_service
from app.services.account_pool_liveness import AccountPoolLivenessService


class OwnerLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.remote = AsyncMock(return_value={'success': True, 'accounts': [
            {'account_id': 'space-1', 'account_user_role': 'account-owner'}]})
        self.teams = SimpleNamespace(
            ensure_access_token=AsyncMock(return_value='token'),
            jwt_parser=SimpleNamespace(extract_email=Mock(return_value='owner@example.com')),
            chatgpt_service=SimpleNamespace(get_account_info=self.remote),
        )
        self.credentials = SimpleNamespace(get_credentials=AsyncMock(return_value=None))
        self.authorization = SimpleNamespace(scan_entry=AsyncMock())
        self.service = AccountPoolLivenessService(self.credentials, self.authorization, self.teams)
        async with self.sessions() as db:
            db.add(Team(id=1, email=' Owner@Example.com ', account_id='space-1', access_token_encrypted='stored-token'))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_virtual_owner_is_checked_persisted_and_never_added_to_pool(self):
        async with self.sessions() as db:
            self.assertEqual(await self.service.check_entry(db, -1), 'alive')
        async with self.sessions() as db:
            row = (await account_pool_service.rows_by_ids(db, [-1]))[0]
            self.assertEqual(row['liveness_status'], 'alive')
            self.assertIn('未验证密码', row['liveness_message'])
            self.assertIsNotNone(row['liveness_checked_at'])
            self.assertEqual(await db.scalar(select(func.count(AccountPoolEntry.id))), 0)
        self.authorization.scan_entry.assert_not_awaited()

    async def test_identity_mismatch_never_calls_upstream(self):
        self.teams.jwt_parser.extract_email.return_value = 'another@example.com'
        async with self.sessions() as db:
            self.assertEqual(await self.service.check_entry(db, -1), 'invalid')
            self.assertIn('不一致', (await db.get(Team, 1)).owner_liveness_message)
        self.remote.assert_not_awaited()

    async def test_denial_network_and_missing_team_are_not_reported_alive(self):
        for response, expected in [
            ({'success': False, 'status_code': 401}, 'invalid'),
            ({'success': False, 'status_code': 403}, 'error'),
            ({'success': True, 'accounts': []}, 'error'),
        ]:
            self.remote.return_value = response
            async with self.sessions() as db:
                self.assertEqual(await self.service.check_entry(db, -1), expected)
                self.assertEqual((await db.get(Team, 1)).owner_liveness_status, expected)
        self.remote.side_effect = ConnectionError('unavailable')
        with self.assertLogs('app.services.account_pool_owner_liveness', level='ERROR'):
            async with self.sessions() as db:
                self.assertEqual(await self.service.check_entry(db, -1), 'error')
                self.assertIsNotNone((await db.get(Team, 1)).owner_liveness_checked_at)

    async def test_all_checks_deduplicate_owner_email_and_include_real_owner_without_password(self):
        from unittest.mock import patch
        from app import main
        from app.routes import admin
        async with self.sessions() as db:
            db.add(Team(id=2, email='owner@example.com', account_id='space-2', access_token_encrypted='stored-token'))
            await db.commit()
        with patch.object(admin, 'account_pool_liveness_service', self.service), patch.object(main, 'AsyncSessionLocal', self.sessions):
            self.assertEqual((await main.scheduled_account_pool_liveness())['alive'], 1)
        self.remote.assert_awaited_once()
        async with self.sessions() as db:
            db.add(AccountPoolEntry(id=10, email='owner@example.com'))
            await db.commit()
        self.remote.reset_mock()
        self.assertEqual((await self.service.check_all(self.sessions))['alive'], 1)
        self.remote.assert_awaited_once()
        async with self.sessions() as db:
            self.assertEqual((await account_pool_service.rows_by_ids(db, [10]))[0]['liveness_status'], 'alive')

    async def test_concurrent_token_edit_does_not_save_stale_result(self):
        async def change(_token, db, **kwargs):
            team = await db.get(Team, 1)
            team.access_token_encrypted = 'new-token'
            await db.commit()
            return {'success': True, 'accounts': [{'account_id': 'space-1', 'account_user_role': 'account-owner'}]}
        self.remote.side_effect = change
        async with self.sessions() as db:
            self.assertIsNone(await self.service.check_entry(db, -1))
            self.assertIsNone((await db.get(Team, 1)).owner_liveness_checked_at)

    async def test_single_account_route_supports_owner_and_does_not_run_all(self):
        from unittest.mock import patch
        from app.routes import admin
        from fastapi import HTTPException
        with patch.object(admin, 'account_pool_liveness_service', self.service):
            async with self.sessions() as db:
                response = await admin.check_account_pool_entry_liveness(-1, db, {})
                self.assertEqual(json.loads(response.body)['status'], 'alive')
                self.assertEqual(response.headers['cache-control'], 'no-store')
                with self.assertRaises(HTTPException) as missing:
                    await admin.check_account_pool_entry_liveness(-999, db, {})
                self.assertEqual(missing.exception.status_code, 404)
        self.remote.assert_awaited_once()
