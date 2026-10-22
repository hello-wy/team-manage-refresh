"""Existing standard seats receive the opt-out default during startup migration."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine

from app.database import Base
from app.db_migrations import run_auto_migration
from app.models import TeamEmailMapping


class StandardSeatMigrationTests(unittest.TestCase):
    def test_existing_rows_default_to_disabled_and_repeat_migration_preserves_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            engine.dispose()
            with sqlite3.connect(path) as connection:
                connection.execute("ALTER TABLE team_email_mappings DROP COLUMN standard_auto_kick_enabled")
                connection.execute(
                    "INSERT INTO team_email_mappings (id, team_id, email, status, source, seat_type, "
                    "auto_kick_at, auto_kick_exempt, is_admin_invited, replacement_export_pending, missing_sync_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (1, 1, "legacy@example.com", "joined", "sync", "standard", "2026-01-01", 0, 0, 0, 0),
                )
            with patch("app.config.settings.database_url", f"sqlite+aiosqlite:///{path}"):
                run_auto_migration()
                with sqlite3.connect(path) as connection:
                    row = connection.execute(
                        "SELECT standard_auto_kick_enabled, auto_kick_at FROM team_email_mappings WHERE id=?", (1,),
                    ).fetchone()
                    self.assertEqual(row, (0, "2026-01-01"))
                    connection.execute(
                        "UPDATE team_email_mappings SET standard_auto_kick_enabled=? WHERE id=?", (1, 1),
                    )
                run_auto_migration()
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT standard_auto_kick_enabled FROM team_email_mappings WHERE id=?", (1,),
                ).fetchone(), (1,))
