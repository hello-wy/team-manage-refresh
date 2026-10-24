import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.database import Base
from app.models import AccountPoolEntry, AccountPoolWorkspace
from app.services.account_pool_authorization import AccountPoolAuthorizationService
from app.services.account_pool_credentials import AccountPoolCredentialService
from app.services.account_pool_liveness import AccountPoolLivenessService
from app.services.account_pool_profile import account_name_key, credential_account_name, save_import_account_name
from app.services.encryption import encryption_service

EMAIL = "member@example.com"
NAME = "Kohei Booker"
TOKEN_EXPIRY = 2_000_000_000


def login_tokens(*, name=NAME, email=EMAIL):
    claims = {"email": email, "name": name, "exp": TOKEN_EXPIRY,
              "https://api.openai.com/auth": {"chatgpt_account_id": "team", "chatgpt_user_id": "user"}}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"access_token": f"header.{encoded}.signature", "id_token": f"header.{encoded}.signature",
            "refresh_token": "test-refresh", "workspace": {"workspace_id": "team", "status": "workspace_ok"}}


class AccountNameExtractionTests(unittest.TestCase):
    def test_uses_authenticated_profile_name_instead_of_workspace_or_export_label(self):
        tokens = login_tokens()
        payload = {"accounts": [{"name": "export-label@example.com", "credentials": tokens}]}
        self.assertEqual(credential_account_name(payload, EMAIL), NAME)
        self.assertEqual(account_name_key("  Ｋｏｈｅｉ  BOOKER  "), "kohei booker")

    def test_mismatched_email_and_invalid_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "邮箱与号池账号不一致"):
            credential_account_name(login_tokens(email="other@example.com"), EMAIL)
        for value in (True, {"name": "invalid"}, "n" * 256):
            with self.subTest(value=value), self.assertRaises(ValueError):
                credential_account_name(login_tokens(name=value), EMAIL)

    def test_missing_name_is_not_invented_from_email(self):
        self.assertIsNone(credential_account_name(login_tokens(name=None), EMAIL))


class AccountPoolNamePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.sessions()
        self.db.add(AccountPoolEntry(id=1, email=EMAIL, password_encrypted="password",
                                     two_factor_secret_encrypted="secret"))
        await self.db.commit()
        self.auth = AccountPoolAuthorizationService(Mock(), Mock(), Mock(), team_service=Mock())

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    def result(self, name=NAME):
        return self.auth._result(EMAIL, login_tokens(name=name), "team")

    async def test_import_saves_name_once_and_normal_login_preserves_it(self):
        self.assertTrue(await save_import_account_name(self.db, 1, self.result().payload))
        await self.db.commit()
        self.assertFalse(await save_import_account_name(self.db, 1, self.result("New Name").payload))
        await self.auth.save_result(self.db, 1, self.result("New Name"))
        entry = await self.db.get(AccountPoolEntry, 1)
        self.assertEqual((entry.name, entry.name_key), (NAME, "kohei booker"))

    async def test_normal_login_does_not_populate_missing_name(self):
        await self.auth.save_result(self.db, 1, self.result())
        self.assertIsNone((await self.db.get(AccountPoolEntry, 1)).name)

    async def test_manual_liveness_does_not_populate_missing_name(self):
        provider = SimpleNamespace(
            scan_entry=AsyncMock(return_value={"available_workspaces": [{"id": "team"}]}),
            login_entry=AsyncMock(return_value=self.result()), save_result=self.auth.save_result,
        )
        credentials = Mock(get_credentials=AsyncMock(return_value={"password": "saved"}))
        status = await AccountPoolLivenessService(credentials, provider).check_entry(self.db, 1)
        self.assertEqual(status, "alive")
        self.assertIsNone((await self.db.get(AccountPoolEntry, 1)).name)

    async def test_concurrent_credential_change_does_not_save_stale_name(self):
        saved = await self.auth.save_result(self.db, 1, self.result(), credential_version=("old", "secret"))
        self.assertFalse(saved)
        self.assertIsNone((await self.db.get(AccountPoolEntry, 1)).name)

    async def test_name_column_escapes_profile_text(self):
        from app.routes.admin import account_pool_page
        await save_import_account_name(self.db, 1, self.result("<Kohei>").payload)
        await self.db.commit()
        request = Request({"type": "http", "method": "GET", "path": "/admin/account-pool",
                           "headers": [], "query_string": b"", "server": ("localhost", 8008)})
        page = await account_pool_page(request, db=self.db, current_user={"username": "admin"})
        html = page.body.decode()
        self.assertIn('data-sort-by="name"', html)
        self.assertIn('data-label="名称">&lt;Kohei&gt;</td>', html)
        self.assertNotIn('data-label="名称"><Kohei>', html)


class AccountPoolNameMigrationTests(unittest.TestCase):
    def test_legacy_database_gets_name_columns_and_index_idempotently(self):
        from app.db_migrations import run_auto_migration
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            engine.dispose()
            db = sqlite3.connect(path)
            db.execute("DROP INDEX idx_account_pool_name_key")
            db.execute("ALTER TABLE account_pool_entries DROP COLUMN name")
            db.execute("ALTER TABLE account_pool_entries DROP COLUMN name_key")
            db.execute("INSERT INTO account_pool_entries (email, created_at, updated_at) VALUES (?, ?, ?)",
                       (EMAIL, "2026-10-15", "2026-10-15"))
            db.commit()
            with patch("app.db_migrations.get_db_path", return_value=path):
                run_auto_migration()
                run_auto_migration()
            columns = {row[1] for row in db.execute("PRAGMA table_info(account_pool_entries)")}
            indexes = {row[1] for row in db.execute("PRAGMA index_list(account_pool_entries)")}
            self.assertTrue({"name", "name_key"} <= columns)
            self.assertIn("idx_account_pool_name_key", indexes)
            self.assertEqual(db.execute("SELECT email FROM account_pool_entries").fetchone()[0], EMAIL)
            db.close()
