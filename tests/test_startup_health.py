import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app import database
from app import main
from app import db_migrations


class _AsyncContext:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self):
        self.execute = AsyncMock()
        self.run_sync = AsyncMock()


class _FakeEngine:
    def __init__(self, connection=None, error=None):
        self.connection = connection
        self.error = error

    def begin(self):
        return _AsyncContext(self.connection, self.error)

    def connect(self):
        return _AsyncContext(self.connection, self.error)


class DatabaseCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_db_does_not_run_sqlite_pragma_for_non_sqlite_database(self):
        connection = _FakeConnection()
        engine = _FakeEngine(connection=connection)

        with patch("app.database._is_sqlite", False), patch("app.database.engine", engine):
            await database.init_db()

        connection.execute.assert_not_awaited()
        connection.run_sync.assert_awaited_once()


class MigrationCompatibilityTests(unittest.TestCase):
    def test_get_db_path_returns_none_for_non_sqlite_database(self):
        with patch(
            "app.config.settings.database_url",
            "postgresql+asyncpg://user:password@db.example.com/app",
        ):
            self.assertIsNone(db_migrations.get_db_path())

    def test_get_db_path_returns_none_for_in_memory_sqlite_database(self):
        with patch(
            "app.config.settings.database_url",
            "sqlite+aiosqlite:///:memory:",
        ):
            self.assertIsNone(db_migrations.get_db_path())

    def test_auto_migration_adds_account_pool_replacement_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(db_path)
            connection.executescript("""
                CREATE TABLE teams (
                    id INTEGER PRIMARY KEY,
                    email VARCHAR(255) NOT NULL,
                    access_token_encrypted TEXT NOT NULL
                );
                CREATE TABLE redemption_codes (
                    id INTEGER PRIMARY KEY,
                    code VARCHAR(32) NOT NULL,
                    status VARCHAR(20)
                );
                CREATE TABLE redemption_records (
                    id INTEGER PRIMARY KEY,
                    email VARCHAR(255) NOT NULL,
                    code VARCHAR(32) NOT NULL,
                    team_id INTEGER NOT NULL,
                    account_id VARCHAR(100) NOT NULL
                );
            """)
            connection.close()

            database_url = f"sqlite+aiosqlite:///{db_path}"
            with patch("app.config.settings.database_url", database_url):
                db_migrations.run_auto_migration()

            connection = sqlite3.connect(db_path)
            team_columns = self._columns(connection, "teams")
            mapping_columns = self._columns(connection, "team_email_mappings")
            pool_columns = self._columns(connection, "account_pool_entries")
            connection.close()

            self.assertIn("pending_replacements", team_columns)
            self.assertIn("last_invited_at", mapping_columns)
            self.assertIn("seat_type", pool_columns)
            self.assertIn("password_encrypted", pool_columns)
            self.assertIn("two_factor_secret_encrypted", pool_columns)

    @staticmethod
    def _columns(connection, table_name):
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


class StartupAndHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_fails_fast_when_database_initialization_fails(self):
        with patch(
            "app.main.init_db",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ):
            with self.assertRaisesRegex(RuntimeError, "数据库初始化失败"):
                async with main.lifespan(main.app):
                    pass

    async def test_health_check_returns_503_when_database_cannot_be_reached(self):
        engine = _FakeEngine(error=SQLAlchemyError("database unavailable"))

        with patch("app.main.engine", engine, create=True):
            response = await main.health_check()

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.body, b'{"status":"unhealthy"}')
