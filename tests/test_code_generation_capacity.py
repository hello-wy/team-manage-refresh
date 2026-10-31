import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import RedemptionCode
from app.services.redemption import RedemptionService


class CodeGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.service = RedemptionService()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_single_code_generation_does_not_require_a_team_or_seat_balance(self):
        async with self.session_factory() as db:
            result = await self.service.generate_code_single(
                db, code="UNBOUND-001", pool_type="normal", has_warranty=False,
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["code"], "UNBOUND-001")
            self.assertNotIn("capacity", result)
            stored = await db.get(RedemptionCode, 1)
            self.assertEqual(stored.code, "UNBOUND-001")
            self.assertIsNone(stored.used_team_id)

    async def test_batch_code_generation_does_not_reserve_team_seats(self):
        async with self.session_factory() as db:
            result = await self.service.generate_code_batch(
                db, count=3, pool_type="normal", has_warranty=False,
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["total"], 3)
            self.assertEqual(len(result["codes"]), 3)
            self.assertNotIn("capacity", result)

            stored = (await db.execute(select(RedemptionCode))).scalars().all()
            self.assertEqual(len(stored), 3)


if __name__ == "__main__":
    unittest.main()
