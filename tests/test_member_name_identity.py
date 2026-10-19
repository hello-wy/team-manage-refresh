import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, MemberAuthorization, Team, TeamEmailMapping
from app.services.account_pool_profile import account_name_key
from app.services.encryption import encryption_service
from app.services.team import TeamService
from test_account_pool_names import login_tokens

EMAIL = "member@example.com"
NAME = "Kohei Booker"
JOINED_AT = "2026-10-15T17:49:00"


class MemberNameIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.sessions()
        self.db.add(Team(id=1, email="owner@example.com", account_id="team",
                         access_token_encrypted="unused", max_members=5, member_auto_kick_hours=2))
        self.db.add(AccountPoolEntry(email=EMAIL, name=NAME, name_key=account_name_key(NAME)))
        await self.db.commit()
        self.remote = SimpleNamespace(
            get_members=AsyncMock(), get_invites=AsyncMock(return_value={"success": True, "items": []}),
            get_seat_capacity=AsyncMock(return_value={"success": True, "data": {"seat_capacity": [
                {"type": "default", "paid": 5, "available": 4},
                {"type": "premium", "paid": 0, "available": 0},
            ]}}),
        )
        self.service = TeamService()
        self.service.chatgpt_service = self.remote
        self.service.ensure_access_token = AsyncMock(return_value="test-token")

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def snapshot(self, *, names=(NAME,), email=None):
        raw = [{"id": "user" if index == 0 else f"other-{index}", "name": name,
                "email": email, "created_time": JOINED_AT, "role": "standard-user", "seat_type": "default"}
               for index, name in enumerate(names)]
        self.remote.get_members.return_value = {"success": True, "members": raw, "total": len(raw)}
        result = await self.service.get_team_members(1, self.db)
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(raw[0]["email"], email)
        return result["members"]

    async def test_unique_name_resolves_email_and_persists_real_join_time_and_deadline(self):
        member, = await self.snapshot(names=("  Ｋｏｈｅｉ  BOOKER ",))
        self.assertEqual(member["email"], EMAIL)
        self.assertIsNone(member["profile_sync_error"])
        self.assertEqual(member["auto_kick_at"], "2026-10-15T19:49:00")
        async with self.sessions() as verification:
            mapping = (await verification.execute(select(TeamEmailMapping))).scalar_one()
            self.assertEqual(mapping.email, EMAIL)
            self.assertEqual(mapping.joined_at, datetime.fromisoformat(JOINED_AT))

    async def test_duplicate_pool_names_are_explicit_and_do_not_create_mapping(self):
        self.db.add(AccountPoolEntry(email="duplicate@example.com", name=NAME, name_key=account_name_key(NAME)))
        await self.db.commit()
        member, = await self.snapshot()
        self.assertIsNone(member["email"])
        self.assertIn("号池中存在同名账号", member["profile_sync_error"])
        self.assertEqual((await self.db.execute(select(TeamEmailMapping))).scalars().all(), [])

    async def test_duplicate_team_names_cannot_both_claim_one_pool_email(self):
        members = await self.snapshot(names=(NAME, NAME))
        self.assertTrue(all(member["email"] is None for member in members))
        self.assertTrue(all("Team 内存在同名成员" in member["profile_sync_error"] for member in members))

    async def test_saved_user_identity_takes_precedence_over_a_matching_name(self):
        self.db.add(MemberAuthorization(team_id=1, account_id="team", email="actual@example.com",
                                        credentials_encrypted=encryption_service.encrypt_token(
                                            json.dumps(login_tokens(email="actual@example.com")))))
        await self.db.commit()
        member, = await self.snapshot()
        self.assertEqual(member["email"], "actual@example.com")

    async def test_explicit_upstream_email_is_never_replaced_by_a_name_match(self):
        member, = await self.snapshot(email="actual@example.com")
        self.assertEqual(member["email"], "actual@example.com")

    async def test_unknown_name_and_deleted_pool_entries_remain_unresolved(self):
        unknown, = await self.snapshot(names=("Unknown Name",))
        self.assertIsNone(unknown["email"])
        entry = (await self.db.execute(select(AccountPoolEntry))).scalar_one()
        entry.deleted_at = datetime(2026, 10, 15)
        await self.db.commit()
        deleted, = await self.snapshot()
        self.assertIsNone(deleted["email"])
