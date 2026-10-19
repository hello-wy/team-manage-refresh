"""Owners shown from Team records can create and complete saved credentials."""
import unittest

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.dependencies.auth import require_admin
from app.main import app
from app.models import AccountPoolEntry, Team
from app.services.account_pool import account_pool_service
from app.services.account_pool_credentials import (
    AccountPoolCredentialError, account_pool_credential_service,
)
from app.services.encryption import encryption_service
from app.utils.time_utils import get_now

EMAIL = "owner@example.com"
SECRET = "JBSWY3DPEHPK3PXP"


class OwnerCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.db.add(Team(id=1, email=" Owner@Example.com ", team_name="Owner Team",
                         account_id="owner-space", access_token_encrypted="existing-token"))
        await self.db.commit()
        self.service = account_pool_credential_service

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def entry_count(self):
        return (await self.db.execute(select(func.count(AccountPoolEntry.id)))).scalar_one()

    async def test_reading_unsaved_owner_returns_empty_fields_without_creating_an_entry(self):
        result = await self.service.get_credentials(self.db, " OWNER@example.com ")
        self.assertEqual(result, {"email": EMAIL, "password": "", "two_factor_secret": ""})
        self.assertEqual(await self.entry_count(), 0)
        self.assertIsNone(await self.service.get_credentials(self.db, "unknown@example.com"))

    async def test_saving_virtual_owner_creates_encrypted_credentials_and_preserves_team(self):
        saved = await self.service.update_credentials(
            self.db, -1, password="new-password", two_factor_secret=SECRET,
        )
        entry = (await self.db.execute(select(AccountPoolEntry))).scalar_one()
        self.assertEqual(saved, {"email": EMAIL, "password": "new-password", "two_factor_secret": SECRET})
        self.assertGreater(entry.id, 0)
        self.assertNotIn("new-password", entry.password_encrypted)
        self.assertNotIn(SECRET, entry.two_factor_secret_encrypted)
        team = await self.db.get(Team, 1)
        self.assertEqual(team.access_token_encrypted, "existing-token")
        listing = await account_pool_service.list_entries(self.db)
        self.assertEqual(listing["total"], 1)
        self.assertTrue(listing["entries"][0]["is_owner"])
        self.assertFalse(listing["entries"][0]["is_virtual_owner"])
        self.assertTrue(listing["entries"][0]["has_login_credentials"])

    async def test_partial_saves_and_stale_virtual_ids_update_the_same_owner_account(self):
        first = await self.service.update_credentials(self.db, -1, password="password-only")
        self.assertEqual(first["two_factor_secret"], "")
        self.db.add(Team(id=2, email=EMAIL, account_id="second-space",
                         access_token_encrypted="second-token"))
        await self.db.commit()
        second = await self.service.update_credentials(self.db, -2, two_factor_secret=SECRET)
        self.assertEqual(second["password"], "password-only")
        self.assertEqual(second["two_factor_secret"], SECRET)
        self.assertEqual(await self.entry_count(), 1)

    async def test_owner_can_save_only_two_factor_first(self):
        saved = await self.service.update_credentials(self.db, -1, two_factor_secret=SECRET)
        self.assertEqual(saved["password"], "")
        self.assertEqual(saved["two_factor_secret"], SECRET)

    async def test_invalid_credentials_do_not_create_or_modify_an_owner_entry(self):
        for changes in ({"password": "new", "two_factor_secret": "INVALID*SECRET"},
                        {"password": ""}, {}):
            with self.assertRaises(AccountPoolCredentialError):
                await self.service.update_credentials(self.db, -1, **changes)
            await self.db.commit()
            self.assertEqual(await self.entry_count(), 0)

    async def test_unknown_owner_id_cannot_create_an_account(self):
        self.assertIsNone(await self.service.update_credentials(self.db, -999, password="new"))
        self.assertEqual(await self.entry_count(), 0)

    async def test_deleted_owner_is_restored_without_resurrecting_unseen_credentials(self):
        entry = AccountPoolEntry(email=EMAIL, deleted_at=get_now(),
                                 two_factor_secret_encrypted=encryption_service.encrypt_token(SECRET))
        self.db.add(entry)
        await self.db.commit()
        original_id = entry.id
        self.assertEqual((await self.service.get_credentials(self.db, EMAIL))["two_factor_secret"], "")
        saved = await self.service.update_credentials(self.db, -1, password="restored")
        self.assertEqual(saved["two_factor_secret"], "")
        self.assertIsNone(entry.deleted_at)
        self.assertEqual(entry.id, original_id)
        self.assertEqual(await self.entry_count(), 1)

    async def test_existing_empty_pool_entry_can_be_completed(self):
        entry = AccountPoolEntry(email=EMAIL)
        self.db.add(entry)
        await self.db.commit()
        saved = await self.service.update_credentials(
            self.db, entry.id, password="new-password", two_factor_secret=SECRET,
        )
        self.assertEqual(saved["password"], "new-password")
        self.assertEqual(saved["two_factor_secret"], SECRET)
        self.assertEqual(await self.entry_count(), 1)

    async def test_http_read_save_reload_and_invalid_owner(self):
        async def database():
            yield self.db

        app.dependency_overrides[get_db] = database
        app.dependency_overrides[require_admin] = lambda: {"username": "admin", "is_admin": True}
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/admin/account-pool/credentials", params={"email": EMAIL})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"]["password"], "")
                self.assertEqual(response.headers["cache-control"], "no-store")
                response = await client.patch("/admin/account-pool/-1/credentials", json={
                    "password": "saved-password", "two_factor_secret": SECRET,
                })
                self.assertEqual(response.status_code, 200)
                response = await client.get("/admin/account-pool/credentials", params={"email": EMAIL})
                self.assertEqual(response.json()["data"]["password"], "saved-password")
                self.assertEqual(response.json()["data"]["two_factor_secret"], SECRET)
                response = await client.patch("/admin/account-pool/-999/credentials", json={"password": "new"})
                self.assertEqual(response.status_code, 404)
        finally:
            app.dependency_overrides.clear()
