import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.dependencies.auth import require_admin
from app.main import app
from app.models import RotationSeatSnapshot, Team
from app.routes import admin
from app.services.seat_snapshot import attach_seat_snapshots, refresh_list_seat_balance
from app.utils.time_utils import get_now


BALANCE = {"standard": {"known": True, "paid": 5, "remaining": 3},
           "premium": {"known": True, "paid": 2, "remaining": 1}}


class ListSeatBalanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.db.add(Team(id=1, email='owner@example.com', access_token_encrypted='encrypted',
                         joined_members=2, total_seats=7))
        await self.db.commit()
        self.service = SimpleNamespace(get_team_seat_balance=AsyncMock(return_value={
            'success': True, 'balance': BALANCE}))

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def snapshot(self, age_minutes=0):
        row = RotationSeatSnapshot(team_id=1, standard_paid=2, standard_remaining=0,
                                   premium_paid=0, premium_remaining=0,
                                   observed_at=get_now() - timedelta(minutes=age_minutes))
        self.db.add(row)
        await self.db.commit()
        return row

    async def test_first_list_visit_fetches_balance_and_persists_without_member_listing(self):
        result = await refresh_list_seat_balance(self.service, 1, self.db)
        self.assertTrue(result['success'])
        self.assertEqual(result['seat_balance']['balance'], BALANCE)
        stored = await self.db.get(RotationSeatSnapshot, 1)
        self.assertEqual((stored.standard_remaining, stored.premium_remaining), (3, 1))
        self.assertEqual(result['joined_members'], 2)
        await refresh_list_seat_balance(self.service, 1, self.db)
        self.service.get_team_seat_balance.assert_awaited_once()

    async def test_recent_snapshots_are_returned_without_remote_requests(self):
        await self.snapshot()
        result = await refresh_list_seat_balance(self.service, 1, self.db)
        self.assertEqual(result['seat_balance']['balance']['standard']['remaining'], 0)
        self.service.get_team_seat_balance.assert_not_awaited()

    async def test_stale_snapshot_is_refreshed_and_list_marks_only_stale_or_missing_rows(self):
        await self.snapshot(age_minutes=6)
        rows = [{'id': 1}, {'id': 2}]
        await attach_seat_snapshots(self.db, rows)
        self.assertTrue(all(row['seat_balance_needs_refresh'] for row in rows))
        await refresh_list_seat_balance(self.service, 1, self.db)
        await attach_seat_snapshots(self.db, rows)
        self.assertFalse(rows[0]['seat_balance_needs_refresh'])
        self.assertTrue(rows[1]['seat_balance_needs_refresh'])

    async def test_failed_or_incomplete_reads_preserve_the_last_confirmed_snapshot(self):
        original = await self.snapshot(age_minutes=6)
        timestamp = original.observed_at
        for result in ({'success': False, 'error': 'unavailable'},
                       {'success': True, 'balance': {'standard': BALANCE['standard']}}):
            self.service.get_team_seat_balance.return_value = result
            self.assertFalse((await refresh_list_seat_balance(self.service, 1, self.db))['success'])
            stored = await self.db.get(RotationSeatSnapshot, 1)
            self.assertEqual(stored.standard_remaining, 0)
            self.assertEqual(stored.observed_at, timestamp)

    async def test_nonexistent_team_does_not_call_remote(self):
        self.assertFalse((await refresh_list_seat_balance(self.service, 99, self.db))['success'])
        self.service.get_team_seat_balance.assert_not_awaited()


class ListSeatBalanceRouteTests(unittest.TestCase):
    def test_route_requires_admin_and_does_not_cache_http_responses(self):
        previous = app.dependency_overrides.copy()
        try:
            with patch.object(admin, 'refresh_list_seat_balance', new=AsyncMock(return_value={'success': True})) as refresh:
                client = TestClient(app)
                self.assertIn(client.post('/admin/teams/1/seats/balance').status_code, (401, 403))
                refresh.assert_not_awaited()
                app.dependency_overrides[require_admin] = lambda: {'is_admin': True}
                app.dependency_overrides[get_db] = lambda: None
                response = client.post('/admin/teams/1/seats/balance')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                refresh.assert_awaited_once_with(admin.team_service, 1, None)
                client.close()
        finally:
            app.dependency_overrides = previous
