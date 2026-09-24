import unittest
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import RedemptionCode, Team
from app.services.redemption import RedemptionService
from app.services.team import TeamService
from app.utils.time_utils import get_now


class AdminListQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_team_stats_aggregate_respects_pool_and_availability(self):
        async with self.sessions() as session:
            session.add_all([
                Team(email="a@example.com", access_token_encrypted="x",
                     status="active", current_members=1, max_members=2, pool_type="normal"),
                Team(email="b@example.com", access_token_encrypted="x",
                     status="active", current_members=2, max_members=2, pool_type="normal"),
                Team(email="c@example.com", access_token_encrypted="x",
                     status="banned", current_members=1, max_members=2, pool_type="normal"),
                Team(email="d@example.com", access_token_encrypted="x",
                     status="expired", current_members=1, max_members=2, pool_type="welfare"),
            ])
            await session.commit()
            stats = await TeamService().get_stats(session, pool_type="normal")

        self.assertEqual(stats, {
            "total": 3, "available": 1, "live": 2, "banned": 1, "expired": 0
        })

    async def test_code_list_syncs_status_and_counts_only_selected_pool(self):
        now = get_now()
        async with self.sessions() as session:
            session.add_all([
                RedemptionCode(code="NORMAL-EXPIRED", pool_type="normal",
                               status="unused", expires_at=now - timedelta(days=1)),
                RedemptionCode(code="NORMAL-VALID", pool_type="normal",
                               status="expired", expires_at=now + timedelta(days=1)),
                RedemptionCode(code="WELFARE-CODE", pool_type="welfare", status="unused"),
            ])
            await session.commit()
            result = await RedemptionService().get_all_codes(
                session, page=1, per_page=20, pool_type="normal"
            )

        self.assertEqual(result["total"], 2)
        self.assertEqual({code["code"]: code["status"] for code in result["codes"]}, {
            "NORMAL-EXPIRED": "expired", "NORMAL-VALID": "unused"
        })
