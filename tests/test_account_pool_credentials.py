import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.database import get_db
from app.dependencies.auth import require_admin
from app.main import app
from app.models import AccountPoolEntry, AccountPoolHistory
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
            session.add(AccountPoolEntry(email="member@example.com", seat_type="default"))
            await session.commit()

            result = await self.service.import_export_names(
                session,
                names=["member@example.com----new-password----NEWSECRET"],
                seat_type="premium",
            )
            entry = (await session.execute(select(AccountPoolEntry))).scalar_one()

            self.assertEqual(result["updated"], ["member@example.com"])
            self.assertEqual(entry.seat_type, "premium")
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

    async def test_delete_removes_entry_and_history(self):
        async with self.sessions() as session:
            entry = AccountPoolEntry(email="member@example.com", seat_type="default")
            session.add(entry)
            await session.flush()
            session.add(
                AccountPoolHistory(
                    account_pool_id=entry.id,
                    joined_at=get_now(),
                )
            )
            await session.commit()

            self.assertTrue(await self.service.delete_entry(session, entry.id))
            self.assertIsNone(await session.get(AccountPoolEntry, entry.id))
            histories = (await session.execute(select(AccountPoolHistory))).scalars().all()
            self.assertEqual(histories, [])


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


if __name__ == "__main__":
    unittest.main()
