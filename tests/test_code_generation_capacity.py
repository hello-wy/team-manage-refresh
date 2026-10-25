import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import RedemptionCode, Team
from app.services.redemption import RedemptionService


class CodeGenerationCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.service = RedemptionService()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_unused_codes_reserve_only_matching_standard_seat_capacity(self):
        async with self.session_factory() as db:
            db.add_all([
                Team(id=1, email="normal@example.com", access_token_encrypted="token",
                     account_id="normal", status="active", pool_type="normal",
                     warranty_seat_enabled=False, current_members=1, max_members=5),
                Team(id=2, email="warranty@example.com", access_token_encrypted="token",
                     account_id="warranty", status="active", pool_type="normal",
                     warranty_seat_enabled=True, current_members=1, max_members=5),
                Team(id=3, email="welfare@example.com", access_token_encrypted="token",
                     account_id="welfare", status="active", pool_type="welfare",
                     warranty_seat_enabled=False, current_members=1, max_members=5),
                RedemptionCode(code="NORMAL-RESERVED-001", status="unused", pool_type="normal",
                               has_warranty=False),
            ])
            await db.commit()
            balance = AsyncMock(side_effect=[
                {"success": True, "balance": {"standard": {"known": True, "remaining": 2}}},
                {"success": True, "balance": {"standard": {"known": True, "remaining": 1}}},
            ])
            team_service = type("TeamServiceStub", (), {"get_team_seat_balance": balance})()

            normal = await self.service.check_standard_seat_capacity(
                db, count=2, has_warranty=False, team_service=team_service,
            )
            self.assertFalse(normal["success"])
            self.assertEqual(normal["available"], 1)
            self.assertEqual(normal["reserved_codes"], 1)
            self.assertEqual(normal["workspaces"], [{
                "team_id": 1,
                "workspace_id": "normal",
                "workspace_name": "normal@example.com",
                "standard_remaining": 1,
            }])
            self.assertIn("本次需要 2", normal["error"])
            balance.assert_awaited_once()

            warranty = await self.service.check_standard_seat_capacity(
                db, count=1, has_warranty=True, team_service=team_service,
            )
            self.assertTrue(warranty["success"])
            self.assertEqual(warranty["available"], 1)
            self.assertEqual(balance.await_count, 2)

    async def test_unknown_standard_balance_blocks_code_generation(self):
        async with self.session_factory() as db:
            db.add(Team(id=4, email="owner@example.com", access_token_encrypted="token",
                        account_id="owner", status="active", pool_type="normal",
                        warranty_seat_enabled=False, current_members=0, max_members=5))
            await db.commit()
            team_service = type("TeamServiceStub", (), {
                "get_team_seat_balance": AsyncMock(return_value={
                    "success": True,
                    "balance": {"standard": {"known": False, "remaining": None}},
                })
            })()

            result = await self.service.check_standard_seat_capacity(
                db, count=1, has_warranty=False, team_service=team_service,
            )

            self.assertFalse(result["success"])
            self.assertEqual(result["available"], 0)
            self.assertIn("余额未知", result["error"])

    async def test_batch_generation_rejects_before_writing_codes_when_capacity_is_insufficient(self):
        async with self.session_factory() as db:
            db.add(Team(id=5, email="owner@example.com", access_token_encrypted="token",
                        account_id="owner", status="active", pool_type="normal",
                        warranty_seat_enabled=False, current_members=0, max_members=5))
            await db.commit()
            team_service = type("TeamServiceStub", (), {
                "get_team_seat_balance": AsyncMock(return_value={
                    "success": True,
                    "balance": {"standard": {"known": True, "remaining": 1}},
                })
            })()

            with patch("app.services.team.TeamService", return_value=team_service):
                result = await self.service.generate_code_batch(
                    db, count=2, pool_type="normal", has_warranty=False,
                )

            self.assertFalse(result["success"])
            self.assertEqual(result["total"], 0)
            self.assertIn("可用标准席位不足", result["error"])
            self.assertIsNone((await db.get(RedemptionCode, 1)))

    async def test_concurrent_generation_cannot_reserve_the_same_standard_seat_twice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(f"sqlite+aiosqlite:///{Path(temp_dir) / 'codes.db'}")
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as db:
                db.add(Team(id=6, email="owner@example.com", access_token_encrypted="token",
                            account_id="owner", status="active", pool_type="normal",
                            warranty_seat_enabled=False, current_members=0, max_members=5))
                await db.commit()

            # Real balance reads commit display/token updates internally. They
            # must finish before taking the lock used for counting reservations.
            async def read_balance(team, db):
                await db.commit()
                await asyncio.sleep(0)
                return {
                    "success": True,
                    "balance": {"standard": {"known": True, "remaining": 1}},
                }

            team_service = type("TeamServiceStub", (), {
                "get_team_seat_balance": AsyncMock(side_effect=read_balance)
            })()

            async def generate_one():
                async with session_factory() as db:
                    return await self.service.generate_code_batch(
                        db, count=1, pool_type="normal", has_warranty=False,
                    )

            with patch("app.services.team.TeamService", return_value=team_service):
                results = await asyncio.gather(generate_one(), generate_one())

            self.assertEqual(sum(result["success"] for result in results), 1)
            self.assertEqual(sum(result["total"] for result in results), 1)
            generated = next(result for result in results if result["success"])
            self.assertTrue(generated["capacity"]["capacity_known"])
            self.assertEqual(generated["capacity"]["workspaces"][0]["team_id"], 6)
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
