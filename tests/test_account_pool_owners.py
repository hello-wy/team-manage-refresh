import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, Team, TeamAccount, TeamEmailMapping
from app.routes.admin import AccountPoolInviteRequest, invite_account_pool_entry, refresh_account_pool_usage
from app.routes.account_pool_totp import rotate_account_pool_totp
from app.services.account_pool import account_pool_service
from app.services.account_pool_batch import AccountPoolBatchService
from app.services.account_pool_credentials import account_pool_credential_service
from app.services.account_pool_rotation import attach_rotation_status
from app.services.account_pool_usage import account_pool_usage_service
from app.services.encryption import encryption_service


class AccountPoolOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()

    def team(self, id=1, email="owner@example.com", name="示例 Team", space="space-1"):
        return Team(id=id, email=email, team_name=name, account_id=space,
                    access_token_encrypted=encryption_service.encrypt_token("owner-token"))

    async def test_owners_are_deduplicated_searchable_paginated_and_never_inserted(self):
        async with self.sessions() as db:
            db.add_all([self.team(email=" Owner@Example.com "), self.team(2, space="space-2"),
                        AccountPoolEntry(email="member@example.com")])
            await db.commit()
            listing = await account_pool_service.list_entries(db, per_page=1)
            self.assertEqual(listing["total"], 2)
            self.assertEqual(listing["total_pages"], 2)
            owner = (await account_pool_service.list_entries(db, search="OWNER"))["entries"][0]
            self.assertTrue(owner["is_owner"])
            self.assertTrue(owner["is_virtual_owner"])
            self.assertEqual(len(owner["owner_teams"]), 2)
            self.assertEqual((await account_pool_service.list_entries(db, search="owner", status_filter="joined"))["total"], 1)
            self.assertEqual((await db.execute(select(func.count(AccountPoolEntry.id)))).scalar(), 1)
            db.add(AccountPoolEntry(email="owner@example.com"))
            await db.commit()
            owner = (await account_pool_service.list_entries(db, search="owner"))["entries"][0]
            self.assertFalse(owner["is_virtual_owner"])
            self.assertEqual((await account_pool_service.list_entries(db))["total"], 2)

    async def test_owner_cannot_be_deleted_rotated_invited_or_selected_as_replacement(self):
        async with self.sessions() as db:
            owner = AccountPoolEntry(email="owner@example.com", password_encrypted="password", two_factor_secret_encrypted="secret")
            member = AccountPoolEntry(email="member@example.com")
            db.add_all([self.team(), owner, member])
            await db.commit()
            batch = AccountPoolBatchService(self.sessions, AsyncMock(), encryption_service)
            for operation in (
                account_pool_credential_service.delete_entry(db, owner.id),
                batch.delete(db, [owner.id, member.id]), batch.enqueue_rotations(db, [owner.id]),
            ):
                with self.assertRaisesRegex(ValueError, "所有者"):
                    await operation
            skipped = await batch.enqueue_import_rotations(db, [owner.email])
            self.assertEqual(skipped["queued"], 0)
            self.assertIn("所有者", skipped["skipped"][0]["reason"])
            with patch("app.routes.admin.team_service.add_team_members", new=AsyncMock()) as invite:
                response = await invite_account_pool_entry(owner.id, AccountPoolInviteRequest(team_id=1), db, {})
                self.assertEqual(response.status_code, 400)
                invite.assert_not_awaited()
            with patch("app.routes.account_pool_totp.totp_service.rotate", new=AsyncMock()) as rotate:
                response = await rotate_account_pool_totp(owner.id, db, {})
                self.assertEqual(response.status_code, 400)
                rotate.assert_not_awaited()
            self.assertEqual((await account_pool_service.find_replacement_candidate(1, db)).id, member.id)
            options = await account_pool_service.list_invite_options(1, db)
            self.assertEqual([item["id"] for item in options], [member.id])
            rows = await account_pool_service.rows_by_ids(db, [owner.id])
            self.assertEqual((await attach_rotation_status(db, rows))[0]["rotation"]["label"], "所有者保留")
            self.assertIsNotNone(await db.get(AccountPoolEntry, owner.id))

    async def test_team_name_uses_membership_and_saved_account_names_not_uuid(self):
        space = "a08a87ce-e8ed-4316-9e80-66aa15fb745e"
        async with self.sessions() as db:
            member = AccountPoolEntry(email="member@example.com", workspace_id=space, workspace_name=space)
            db.add_all([self.team(), member, TeamAccount(team_id=1, account_id=space, account_name="第二工作区")])
            await db.commit()
            row = (await account_pool_service.rows_by_ids(db, [member.id]))[0]
            self.assertEqual(row["display_team_name"], "第二工作区")
            self.assertEqual(row["workspace_options"][0]["name"], "第二工作区")
            member.workspace_id = "unknown-space"
            row = (await account_pool_service.rows_by_ids(db, [member.id]))[0]
            self.assertEqual(row["display_team_name"], "Team 名称待同步")
            member.workspace_id = None
            db.add(TeamEmailMapping(team_id=1, email=member.email, status="joined"))
            await db.commit()
            row = (await account_pool_service.rows_by_ids(db, [member.id]))[0]
            self.assertEqual(row["display_team_name"], "示例 Team")

    async def test_owner_quota_refresh_uses_own_team_token_without_pool_entry(self):
        async with self.sessions() as db:
            db.add(self.team(email=" Owner@Example.com "))
            await db.commit()
            with patch.object(account_pool_usage_service, "_check_token", new=AsyncMock(return_value={
                "status": "ok", "5h": {"used": 20, "limit": 100, "remaining": 80}, "1week": {},
            })) as check:
                response = await refresh_account_pool_usage(-1, db, {})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn("20%", json.loads(response.body)["html"])
            self.assertEqual(check.await_args.args[0], ("owner-token", "space-1"))
            with self.assertRaises(HTTPException) as missing:
                await refresh_account_pool_usage(-999, db, {})
            self.assertEqual(missing.exception.status_code, 404)

    async def test_member_quota_refresh_never_falls_back_to_another_accounts_token(self):
        async with self.sessions() as db:
            member = AccountPoolEntry(email="member@example.com", workspace_id="space-1")
            db.add_all([self.team(), member])
            await db.commit()
            with patch.object(account_pool_usage_service, "_check_token", new=AsyncMock(return_value={
                "status": "unavailable", "error": "缺少授权",
            })) as check:
                response = await refresh_account_pool_usage(member.id, db, {})
            self.assertIsNone(check.await_args.args[0])
            self.assertIn("待授权", json.loads(response.body)["html"])
