"""Existing members keep their previous timed-departure behaviour when participation is introduced."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine

from app.database import Base
from app.db_migrations import run_auto_migration

JOINED_AT = "2026-10-15T13:13:00"
DEADLINE = "2026-10-15T15:13:00"


class MemberAutoKickMigrationTests(unittest.TestCase):
    def test_only_previously_scheduled_members_keep_participating(self):
        # id, email, status, seat, role, deadline, exempt, standard opt-in, interim explicit plan
        members = [
            (1, "premium@example.com", "joined", "premium", "standard-user", DEADLINE, 0, 0, None),
            (2, "standard-enabled@example.com", "joined", "standard", "standard-user", DEADLINE, 0, 1, None),
            (3, "standard-planned@example.com", "joined", "default", "standard-user", DEADLINE, 0, 0, JOINED_AT),
            (4, "standard-stale@example.com", "joined", "standard", "standard-user", DEADLINE, 0, 0, None),
            (5, "exempt@example.com", "joined", "premium", "standard-user", None, 1, 0, None),
            (6, "unscheduled@example.com", "joined", "premium", "standard-user", None, 0, 0, None),
            (7, "owner@example.com", "joined", "premium", "account-owner", None, 0, 0, None),
            (8, "invited@example.com", "invited", "standard", None, None, 0, 0, None),
        ]
        expected = {1: 1, 2: 1, 3: 1, 4: 0, 5: 0, 6: 0, 7: 1, 8: 1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            engine.dispose()
            with sqlite3.connect(path) as connection:
                connection.execute("ALTER TABLE team_email_mappings DROP COLUMN auto_kick_enabled")
                connection.execute("ALTER TABLE teams DROP COLUMN member_auto_kick_hours")
                connection.execute("ALTER TABLE team_email_mappings ADD COLUMN auto_kick_start_at DATETIME")
                connection.execute(
                    "INSERT INTO teams (id, email, access_token_encrypted, status, pool_type, rotation_mode, "
                    "pending_replacements) VALUES (1, 'team@example.com', 'unused', 'active', 'normal', 'off', 0)"
                )
                connection.executemany(
                    "INSERT INTO team_email_mappings "
                    "(id, team_id, email, status, source, seat_type, member_role, joined_at, auto_kick_at, "
                    "auto_kick_exempt, standard_auto_kick_enabled, auto_kick_start_at, is_admin_invited, "
                    "replacement_export_pending, missing_sync_count) "
                    f"VALUES (?, 1, ?, ?, 'sync', ?, ?, '{JOINED_AT}', ?, ?, ?, ?, 0, 0, 0)",
                    members,
                )

            with patch("app.config.settings.database_url", f"sqlite+aiosqlite:///{path}"):
                run_auto_migration()
                with sqlite3.connect(path) as connection:
                    connection.execute("UPDATE team_email_mappings SET auto_kick_enabled = 1 WHERE id = 4")
                run_auto_migration()

            with sqlite3.connect(path) as connection:
                rows = dict(connection.execute(
                    "SELECT id, auto_kick_enabled FROM team_email_mappings ORDER BY id"
                ).fetchall())
                exempt = dict(connection.execute("SELECT id, auto_kick_exempt FROM team_email_mappings").fetchall())
                hours = connection.execute("SELECT member_auto_kick_hours FROM teams").fetchone()[0]

        self.assertEqual(rows, {**expected, 4: 1})
        self.assertEqual(exempt, {member[0]: member[6] for member in members})
        self.assertEqual(hours, 2)


if __name__ == "__main__":
    unittest.main()
