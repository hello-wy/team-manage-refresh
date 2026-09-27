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

    async def test_capacity_status_matches_list_filters_pagination_and_stats(self):
        cases = [
            # name, stored status, local count, joined, paid, effective status
            ("upstream-full", "active", 2, 2, 2, "full"),
            ("overbooked", "active", 3, 3, 2, "full"),
            ("zero-seats", "active", 0, 0, 0, "full"),
            ("local-full", "active", 6, 2, 10, "full"),
            ("available", "active", 2, 2, 3, "active"),
            ("unknown-seats", "active", 2, 2, None, "active"),
            ("unknown-members", "active", 2, None, 2, "active"),
            ("invalid-seats", "active", 2, 2, -1, "active"),
            ("stored-full", "full", 2, 2, 10, "full"),
            ("banned", "banned", 6, 2, 2, "banned"),
            ("error", "error", 6, 2, 2, "error"),
            ("expired", "expired", 6, 2, 2, "expired"),
        ]
        service = TeamService()
        async with self.sessions() as session:
            for pool in ("normal", "welfare"):
                for name, stored, local, joined, seats, expected in cases:
                    session.add(Team(
                        email=f"{name}-{pool}@example.com", access_token_encrypted="x",
                        status=stored, current_members=local, max_members=6,
                        joined_members=joined, total_seats=seats, pool_type=pool,
                    ))
            await session.commit()
            for pool in ("normal", "welfare"):
                result = await service.get_all_teams(session, per_page=50, pool_type=pool)
                self.assertTrue(result["success"], result)
                self.assertEqual({row["email"]: row["status"] for row in result["teams"]}, {
                    f"{name}-{pool}@example.com": expected
                    for name, _, _, _, _, expected in cases
                })
                for status in ("active", "full", "banned", "error", "expired"):
                    expected_emails = {f"{row[0]}-{pool}@example.com" for row in cases if row[-1] == status}
                    emails = set()
                    for page in range(1, len(expected_emails) + 1):
                        filtered = await service.get_all_teams(
                            session, status=status, pool_type=pool, page=page, per_page=1,
                        )
                        self.assertEqual(filtered["total"], len(expected_emails))
                        emails.update(row["email"] for row in filtered["teams"])
                    self.assertEqual(emails, expected_emails)
                stats = await service.get_stats(session, pool_type=pool)
                self.assertEqual(stats, {"total": 12, "available": 4, "live": 9, "banned": 1, "expired": 1})

    async def test_capacity_status_recovers_after_a_member_leaves(self):
        async with self.sessions() as session:
            team = Team(email="full@example.com", access_token_encrypted="x", status="active",
                        current_members=2, max_members=6, joined_members=2, total_seats=2)
            session.add(team)
            await session.commit()
            self.assertEqual(team.effective_status, "full")
            team.joined_members = 1
            await session.commit()
            result = await TeamService().get_all_teams(session, status="active")
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["teams"][0]["status"], "active")
