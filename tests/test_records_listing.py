import unittest
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import QuotaSnapshot, Sub2apiExportRecord
from app.services.records_listing import list_export_records
from app.utils.time_utils import get_now

TOTAL_RECORDS = 25
PAGE_SIZE = 10


class RecordsListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add_all([
                Sub2apiExportRecord(
                    email=f"{index}@example.com", team_space_id=f"space-{index}"
                )
                for index in range(TOTAL_RECORDS)
            ])
            session.add_all([
                QuotaSnapshot(email=f"{index}@example.com", team_space_id=f"space-{index}",
                              status="ok", observed_at=get_now(),
                              weekly_remaining=0 if index % 2 else 10)
                for index in range(TOTAL_RECORDS)
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_default_page_reads_snapshots_without_live_quota_calls(self):
        usage = AsyncMock()
        async with self.sessions() as session:
            result = await list_export_records(session, {
                "search": "", "joined_filter": "all", "usage_filter": "",
                "page": 2, "per_page": PAGE_SIZE,
            }, usage)
        self.assertEqual(result["pagination"]["total"], TOTAL_RECORDS)
        self.assertEqual(len(result["records"]), PAGE_SIZE)
        usage.check_many.assert_not_awaited()
        self.assertEqual(result["records"][0]["usage"]["status"], "ok")

    async def test_usage_filter_checks_all_candidates_before_pagination(self):
        usage = AsyncMock()
        async with self.sessions() as session:
            result = await list_export_records(session, {
                "search": "", "joined_filter": "all", "usage_filter": "exhausted",
                "page": 1, "per_page": PAGE_SIZE,
            }, usage)
        usage.check_many.assert_not_awaited()
        self.assertEqual(result["pagination"]["total"], TOTAL_RECORDS // 2)
        self.assertEqual(len(result["records"]), PAGE_SIZE)
