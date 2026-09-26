import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.database import get_db
from app.dependencies.auth import require_admin
from app.main import app
from app.models import AccountPoolEntry, AccountPoolHistory, AccountPoolWorkspace, Sub2apiExportRecord
from app.services.account_pool_credentials import (
    AccountPoolCredentialError,
    AccountPoolCredentialService,
)
from app.services.encryption import encryption_service
from app.utils.time_utils import get_now


class AccountPoolCredentialServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.service = AccountPoolCredentialService(encryption_service)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_import_encrypts_and_reads_credentials(self):
        async with self.sessions() as session:
            result = await self.service.import_export_names(
                session,
                names=["Member@Example.com----pass-123----JBSWY3DPEHPK3PXP"],
            )
            entry = (await session.execute(select(AccountPoolEntry))).scalar_one()
            credentials = await self.service.get_credentials(session, "MEMBER@example.com")

            self.assertEqual(result["added"], ["member@example.com"])
            self.assertNotIn("pass-123", entry.password_encrypted)
            self.assertNotIn("JBSWY3DPEHPK3PXP", entry.two_factor_secret_encrypted)
            self.assertEqual(credentials["password"], "pass-123")
            self.assertEqual(credentials["two_factor_secret"], "JBSWY3DPEHPK3PXP")

    async def test_import_updates_existing_entry(self):
        async with self.sessions() as session:
            session.add(AccountPoolEntry(email="member@example.com"))
            await session.commit()

            result = await self.service.import_export_names(
                session,
                names=["member@example.com----new-password----NEWSECRET"],
            )
            entry = (await session.execute(select(AccountPoolEntry))).scalar_one()

            self.assertEqual(result["updated"], ["member@example.com"])
            self.assertEqual(
                (await self.service.get_credentials(session, entry.email))["password"],
                "new-password",
            )

    async def test_invalid_export_name_fails_without_partial_import(self):
        async with self.sessions() as session:
            with self.assertRaisesRegex(AccountPoolCredentialError, "无效"):
                await self.service.import_export_names(
                    session,
                    names=[
                        "ok@example.com----password----SECRET",
                        "missing-fields@example.com----password",
                    ],
                )
            entries = (await session.execute(select(AccountPoolEntry))).scalars().all()
            self.assertEqual(entries, [])

    async def test_delete_removes_entry_and_history_from_database(self):
        async with self.sessions() as session:
            entry = AccountPoolEntry(
                email="member@example.com",
                password_encrypted=encryption_service.encrypt_token("old-password"),
                two_factor_secret_encrypted=encryption_service.encrypt_token("OLDSECRET"),
            )
            session.add(entry)
            await session.flush()
            entry_id = entry.id
            session.add(
                AccountPoolHistory(
                    account_pool_id=entry_id,
                    joined_at=get_now(),
                )
            )
            session.add(Sub2apiExportRecord(
                email=entry.email,
                team_space_id="team-a",
                first_exported_at=get_now(),
                last_exported_at=get_now(),
                created_at=get_now(),
                updated_at=get_now(),
            ))
            await session.commit()

            self.assertTrue(await self.service.delete_entry(session, entry_id))
            deleted_entry = await session.get(AccountPoolEntry, entry_id)
            self.assertIsNone(deleted_entry)
            self.assertIsNone(
                await self.service.get_credentials(session, "member@example.com")
            )
            histories = (await session.execute(select(AccountPoolHistory))).scalars().all()
            self.assertEqual(histories, [])
            exports = (await session.execute(select(Sub2apiExportRecord))).scalars().all()
            self.assertEqual(exports, [])

            result = await self.service.add_accounts(
                session,
                content="member@example.com----new-password----NEWSECRET",
            )
            credentials = await self.service.get_credentials(session, "member@example.com")

            self.assertEqual(result["added"], ["member@example.com"])
            self.assertEqual(credentials["password"], "new-password")
            self.assertEqual(credentials["two_factor_secret"], "NEWSECRET")
            self.assertEqual(len((await session.execute(select(AccountPoolHistory))).scalars().all()), 0)

    async def test_update_credentials_encrypts_and_preserves_omitted_field(self):
        async with self.sessions() as session:
            entry = AccountPoolEntry(
                email="member@example.com",
                password_encrypted=encryption_service.encrypt_token("old-password"),
                two_factor_secret_encrypted=encryption_service.encrypt_token(
                    "JBSWY3DPEHPK3PXP"
                ),
            )
            session.add(entry)
            await session.flush()
            session.add(AccountPoolWorkspace(
                account_pool_id=entry.id, workspace_id="team-a", status="workspace_ok",
                export_json_encrypted=encryption_service.encrypt_token("old-json"),
            ))
            await session.commit()

            result = await self.service.update_credentials(
                session,
                entry.id,
                password="new-password",
            )
            stored = await session.get(AccountPoolEntry, entry.id)

            self.assertEqual(result["password"], "new-password")
            self.assertEqual(result["two_factor_secret"], "JBSWY3DPEHPK3PXP")
            self.assertNotIn("new-password", stored.password_encrypted)
            self.assertEqual((await session.execute(select(AccountPoolWorkspace))).scalars().all(), [])

    async def test_update_credentials_rejects_invalid_two_factor_without_changes(self):
        async with self.sessions() as session:
            entry = AccountPoolEntry(
                email="member@example.com",
                password_encrypted=encryption_service.encrypt_token("old-password"),
            )
            session.add(entry)
            await session.commit()

            with self.assertRaisesRegex(AccountPoolCredentialError, "Base32"):
                await self.service.update_credentials(
                    session,
                    entry.id,
                    password="new-password",
                    two_factor_secret="INVALID*SECRET",
                )
            await session.refresh(entry)
            credentials = await self.service.get_credentials(session, entry.email)

            self.assertEqual(credentials["password"], "old-password")
            self.assertEqual(credentials["two_factor_secret"], "")


class AccountPoolCredentialRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

        async def database():
            yield AsyncMock()

        app.dependency_overrides[get_db] = database

    def tearDown(self):
        app.dependency_overrides.clear()
        self.client.close()

    def test_credentials_endpoint_requires_admin(self):
        response = self.client.get(
            "/admin/account-pool/credentials?email=member@example.com"
        )
        self.assertIn(response.status_code, (401, 403))

    def test_credentials_response_disables_cache(self):
        app.dependency_overrides[require_admin] = lambda: {
            "username": "admin",
            "is_admin": True,
        }
        credentials = {
            "email": "member@example.com",
            "password": "password",
            "two_factor_secret": "SECRET",
        }
        with patch(
            "app.routes.account_pool_credentials.account_pool_credential_service.get_credentials",
            new=AsyncMock(return_value=credentials),
        ):
            response = self.client.get(
                "/admin/account-pool/credentials?email=member@example.com"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["data"], credentials)

    def test_update_credentials_endpoint_returns_saved_values_without_cache(self):
        app.dependency_overrides[require_admin] = lambda: {
            "username": "admin",
            "is_admin": True,
        }
        credentials = {
            "email": "member@example.com",
            "password": "new-password",
            "two_factor_secret": "JBSWY3DPEHPK3PXP",
        }
        with patch(
            "app.routes.account_pool_credentials.account_pool_credential_service.update_credentials",
            new=AsyncMock(return_value=credentials),
        ) as update:
            response = self.client.patch(
                "/admin/account-pool/7/credentials",
                json={
                    "password": "new-password",
                    "two_factor_secret": "JBSWY3DPEHPK3PXP",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["data"], credentials)
        update.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
