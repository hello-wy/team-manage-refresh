"""Official renewal times remain distinct from entitlement expiry in storage and UI."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.db_migrations import run_auto_migration
from app.models import Team
from app.services.team import TeamService

WORKSPACE_ID = "test-renewal-workspace"
OWNER_EMAIL = "owner@example.com"
EXPIRY_UTC = "2026-11-22T17:43:48+00:00"
RENEWAL_UTC = "2026-11-22T11:43:48+00:00"
EXPIRY_LOCAL = datetime(2026, 11, 23, 1, 43, 48)
RENEWAL_LOCAL = datetime(2026, 11, 22, 19, 43, 48)


def render_team_list(teams):
    templates = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "app/templates"),
        autoescape=select_autoescape(),
    )
    templates.filters.update(
        format_datetime=lambda value: datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M"),
        escape_js=json.dumps,
    )
    return templates.get_template("admin/index.html").render(
        user={"username": "test"}, admin_profile={"avatar": "", "nickname": "管理员"},
        active_page="dashboard", stats={"total_teams": len(teams), "available_teams": len(teams)},
        teams=teams,
        pagination={"current_page": 1, "per_page": 20, "total_pages": 1, "total": len(teams)},
    )


class TeamRenewalSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.db.add(Team(id=1, email=OWNER_EMAIL, account_id=WORKSPACE_ID,
                         access_token_encrypted="unused-test-ciphertext", status="active"))
        await self.db.commit()
        self.service = TeamService()
        self.service.ensure_access_token = AsyncMock(return_value="test-access-token")
        self.service.jwt_parser = SimpleNamespace(extract_email=lambda token: OWNER_EMAIL)
        self.remote_account = {
            "account_id": WORKSPACE_ID, "name": "Test", "plan_type": "team",
            "subscription_plan": "team", "expires_at": EXPIRY_UTC, "renews_at": RENEWAL_UTC,
        }
        self.service.chatgpt_service = SimpleNamespace(
            get_account_info=AsyncMock(return_value={"success": True, "accounts": [self.remote_account]}),
            get_members=AsyncMock(return_value={"success": True, "members": [], "total": 0}),
            get_invites=AsyncMock(return_value={"success": True, "items": []}),
            get_seat_capacity=AsyncMock(return_value={"success": True, "data": {}}),
        )

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def sync_and_reload(self):
        with patch("app.config.settings.timezone", "Asia/Shanghai"):
            result = await self.service.sync_team_info(1, self.db)
        self.assertTrue(result["success"], result.get("error"))
        self.db.expunge_all()
        return await self.db.get(Team, 1)

    async def test_sync_persists_independent_local_times_and_renders_them_in_admin_list(self):
        team = await self.sync_and_reload()
        self.assertEqual(team.expires_at, EXPIRY_LOCAL)
        self.assertEqual(team.renews_at, RENEWAL_LOCAL)
        self.db.expunge_all()
        listing = await self.service.get_all_teams(self.db)
        self.assertTrue(listing["success"], listing.get("error"))
        self.assertEqual(listing["teams"][0]["renews_at"], RENEWAL_LOCAL.isoformat() + "+08:00")
        html = render_team_list(listing["teams"])
        self.assertIn('title="官方计划续费时间（北京时间），可能早于权益到期时间">续费时间</th>', html)
        renewal_cell = html.split('<td data-label="续费时间"', 1)[1].split('</td>', 1)[0]
        expiry_cell = html.split('<td data-label="到期时间">', 1)[1].split('</td>', 1)[0]
        self.assertIn("2026-11-22 19:43", renewal_cell)
        self.assertIn('data-renewal-at="2026-11-22T19:43:48+08:00"', renewal_cell)
        self.assertIn('aria-pressed="false"', renewal_cell)
        self.assertIn("2026-11-23 01:43", expiry_cell)

    async def test_missing_renewal_clears_previous_value_without_inventing_a_time(self):
        await self.sync_and_reload()
        del self.remote_account["renews_at"]
        team = await self.sync_and_reload()
        self.assertIsNone(team.renews_at)
        self.assertEqual(team.expires_at, EXPIRY_LOCAL)
        listing = await self.service.get_all_teams(self.db)
        self.assertIsNone(listing["teams"][0]["renews_at"])
        cell = render_team_list(listing["teams"]).split('<td data-label="续费时间"', 1)[1]
        self.assertEqual(cell.split('>', 1)[1].split('</td>', 1)[0].strip(), "-")


class TeamRenewalMigrationTests(unittest.TestCase):
    def test_legacy_database_adds_nullable_renewal_and_repeat_migration_preserves_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(Team.__table__.insert().values(
                    id=1, email=OWNER_EMAIL, access_token_encrypted="unused", expires_at=EXPIRY_LOCAL,
                ))
            engine.dispose()
            with sqlite3.connect(path) as connection:
                connection.execute("ALTER TABLE teams DROP COLUMN renews_at")
                original_expiry = connection.execute("SELECT expires_at FROM teams WHERE id=?", (1,)).fetchone()[0]
            with patch("app.config.settings.database_url", f"sqlite+aiosqlite:///{path}"):
                run_auto_migration()
                with sqlite3.connect(path) as connection:
                    self.assertEqual(connection.execute(
                        "SELECT expires_at, renews_at FROM teams WHERE id=?", (1,),
                    ).fetchone(), (original_expiry, None))
                    connection.execute("UPDATE teams SET renews_at=? WHERE id=?",
                                       (RENEWAL_LOCAL.isoformat(), 1))
                run_auto_migration()
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT expires_at, renews_at FROM teams WHERE id=?", (1,),
                ).fetchone(), (original_expiry, RENEWAL_LOCAL.isoformat()))
