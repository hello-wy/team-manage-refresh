"""升级旧数据库时保留旧计时，不将历史邀请重新激活。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine

import app.models  # 注册完整模型元数据
from app.database import Base
from app.db_migrations import run_auto_migration


class MemberLifecycleMigrationTests(unittest.TestCase):
    def test_legacy_records_keep_existing_times_and_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            engine = create_engine(f'sqlite:///{path}')
            Base.metadata.create_all(engine)
            engine.dispose()
            with sqlite3.connect(path) as connection:
                for table, columns in [('redemption_records', ['activation_pending', 'service_started_at']),
                    ('team_email_mappings', ['join_source', 'departure_policy', 'usage_started_at'])]:
                    for column in columns:
                        connection.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
                connection.execute("INSERT INTO redemption_records (email, code, team_id, account_id, redeemed_at) VALUES ('member@example.com', 'LEGACY', 1, 'space', '2026-10-01 12:00:00')")
            with patch('app.config.settings.database_url', f'sqlite+aiosqlite:///{path}'):
                run_auto_migration()
                run_auto_migration()
            with sqlite3.connect(path) as connection:
                row = connection.execute('SELECT redeemed_at, activation_pending, service_started_at FROM redemption_records').fetchone()
                self.assertEqual(row, ('2026-10-01 12:00:00', 0, None))
                fields = {row[1] for row in connection.execute('PRAGMA table_info(team_email_mappings)')}
                self.assertTrue({'join_source', 'departure_policy', 'usage_started_at'} <= fields)
