import unittest

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Sub2apiExportRecord
from app.services.records_listing import list_export_records

TOTAL_RECORDS = 25
PAGE_SIZE = 10


class UsageStub:
    def __init__(self):
        self.requested = []

    async def check_many(self, db, keys):
        self.requested.append(keys)
        return {key: {
            "status": "ok",
            "1week": {"state": "exhausted" if int(key[0].split("@")[0]) % 2 else "available"},
        } for key in keys}


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
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_default_page_checks_only_visible_accounts(self):
        usage = UsageStub()
        async with self.sessions() as session:
            result = await list_export_records(session, {
                "search": "", "joined_filter": "all", "usage_filter": "",
                "page": 2, "per_page": PAGE_SIZE,
            }, usage)
        self.assertEqual(result["pagination"]["total"], TOTAL_RECORDS)
        self.assertEqual(len(result["records"]), PAGE_SIZE)
        self.assertEqual(len(usage.requested[0]), PAGE_SIZE)

    async def test_usage_filter_checks_all_candidates_before_pagination(self):
        usage = UsageStub()
        async with self.sessions() as session:
            result = await list_export_records(session, {
                "search": "", "joined_filter": "all", "usage_filter": "exhausted",
                "page": 1, "per_page": PAGE_SIZE,
            }, usage)
        self.assertEqual(len(usage.requested[0]), TOTAL_RECORDS)
        self.assertEqual(result["pagination"]["total"], TOTAL_RECORDS // 2)
        self.assertEqual(len(result["records"]), PAGE_SIZE)
