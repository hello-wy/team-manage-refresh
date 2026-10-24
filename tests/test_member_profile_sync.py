"""Regression coverage for member profile and exit schedule synchronization."""
import base64
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models import MemberAuthorization, Team, TeamEmailMapping
from app.services.encryption import encryption_service
from app.services.member_profile import parse_member_joined_at
from app.services.team import TeamService

EMAIL = "member@example.com"
JOINED_AT = "2026-10-15T13:13:00"
AUTO_KICK_HOURS = 2


class MemberProfileSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.sessions()
        self.db.add(Team(id=1, email="owner@example.com", account_id="account-1",
                         access_token_encrypted="encrypted", max_members=5,
                         member_auto_kick_hours=AUTO_KICK_HOURS))
        await self.db.commit()
        self.remote = SimpleNamespace(
            get_members=AsyncMock(),
            get_invites=AsyncMock(return_value={"success": True, "items": []}),
            get_seat_capacity=AsyncMock(return_value={"success": True, "data": {
                "seat_capacity": [{"type": "default", "paid": 5, "available": 4}],
            }}),
        )
        self.service = TeamService()
        self.service.chatgpt_service = self.remote
        self.service.ensure_access_token = AsyncMock(return_value="test-token")

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def fetch(self, **fields):
        member = {"id": "user-1", "email": EMAIL, "name": "成员甲",
                  "role": "standard-user", "seat_type": "prolite",
                  "created_time": JOINED_AT, **fields}
        self.remote.get_members.return_value = {
            "success": True, "members": [member], "total": 1,
        }
        result = await self.service.get_team_members(1, self.db)
        self.assertTrue(result["success"], result.get("error"))
        return result["members"][0]

    async def mapping(self):
        return (await self.db.execute(select(TeamEmailMapping))).scalar_one()

    async def test_every_fetch_persists_name_and_recalculates_deadline(self):
        await self.fetch()
        updated_at = "2026-10-15T14:15:00"
        member = await self.fetch(name="成员乙", created_time=updated_at)
        self.assertEqual(member["name"], "成员乙")
        self.assertEqual(member["added_at"], updated_at)
        expected = datetime.fromisoformat(updated_at) + timedelta(hours=AUTO_KICK_HOURS)
        self.assertEqual(member["auto_kick_at"], expected.isoformat())
        self.assertIsNone(member["profile_sync_error"])
        async with self.sessions() as verification:
            mapping = (await verification.execute(select(TeamEmailMapping))).scalar_one()
            self.assertEqual(mapping.member_name, "成员乙")
            self.assertEqual(mapping.joined_at, datetime.fromisoformat(updated_at))
            self.assertEqual(mapping.auto_kick_at, expected)

    async def test_partial_profile_matches_saved_user_id_and_keeps_schedule(self):
        original = await self.fetch()
        mapping = await self.mapping()
        mapping.auto_kick_at = None
        await self.db.commit()
        member = await self.fetch(email=None, name=None, created_time=None, role=None)
        for key in ("email", "name", "added_at", "auto_kick_at", "role"):
            self.assertEqual(member[key], original[key])
        self.assertIsNone(member["profile_sync_error"])
        self.assertEqual((await self.mapping()).missing_sync_count, 0)
        self.assertEqual(member["profile_cached_fields"], ["邮箱", "姓名", "加入时间", "角色"])

    async def test_missing_profile_is_filled_by_next_fetch(self):
        incomplete = await self.fetch(name=None, created_time=None)
        self.assertIsNone(incomplete["auto_kick_at"])
        self.assertIn("缺少加入时间", incomplete["profile_sync_error"])
        complete = await self.fetch()
        self.assertEqual(complete["name"], "成员甲")
        self.assertEqual(complete["auto_kick_at"], "2026-10-15T15:13:00")
        self.assertIsNone(complete["profile_sync_error"])

    async def test_timestamp_joined_at_and_schedule_use_same_local_time(self):
        timezone = pytz.timezone(settings.timezone)
        joined = timezone.localize(datetime.fromisoformat(JOINED_AT))
        for value in (joined.timestamp(), str(joined.timestamp()),
                      int(joined.timestamp() * 1000)):
            with self.subTest(value=value):
                member = await self.fetch(created_time=value)
                self.assertEqual(member["added_at"], JOINED_AT)
                self.assertEqual(member["auto_kick_at"], "2026-10-15T15:13:00")

    async def test_invalid_time_exposes_error_without_inventing_join_time(self):
        member = await self.fetch(created_time="not-a-time")
        self.assertIsNone(member["added_at"])
        self.assertIsNone(member["auto_kick_at"])
        self.assertIn("格式无效", member["profile_sync_error"])
        self.assertIsNone((await self.mapping()).joined_at)

    async def test_unknown_email_exposes_error_without_creating_fake_mapping(self):
        member = await self.fetch(email=None)
        self.assertIsNone(member["email"])
        self.assertIsNone(member["auto_kick_at"])
        self.assertIn("缺少成员邮箱", member["profile_sync_error"])
        self.assertEqual((await self.db.execute(select(TeamEmailMapping))).scalars().all(), [])

    async def test_saved_member_authorization_resolves_hidden_email_on_every_fetch(self):
        claims = {"email": EMAIL, "https://api.openai.com/auth": {
            "chatgpt_account_id": "account-1", "chatgpt_user_id": "user-1",
        }}
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        credentials = json.dumps({"access_token": f"header.{encoded}.signature"})
        self.db.add(MemberAuthorization(
            team_id=1, account_id="account-1", email=EMAIL,
            credentials_encrypted=encryption_service.encrypt_token(credentials),
        ))
        await self.db.commit()
        member = await self.fetch(email=None)
        self.assertEqual(member["email"], EMAIL)
        self.assertEqual(member["name"], "成员甲")
        self.assertEqual(member["auto_kick_at"], "2026-10-15T15:13:00")
        self.assertIsNone(member["profile_sync_error"])

    async def test_unresolved_member_identity_does_not_imply_a_saved_email_has_left(self):
        self.db.add(TeamEmailMapping(team_id=1, email=EMAIL, status="joined"))
        await self.db.commit()
        for _ in range(3):
            member = await self.fetch(email=None)
            self.assertIn("缺少成员邮箱", member["profile_sync_error"])
        mapping = await self.mapping()
        self.assertEqual(mapping.status, "joined")
        self.assertEqual(mapping.missing_sync_count, 0)

    async def test_rejoin_does_not_reuse_previous_join_time(self):
        await self.fetch()
        mapping = await self.mapping()
        mapping.status = "removed"
        await self.db.commit()
        member = await self.fetch(created_time=None)
        self.assertIsNone(member["added_at"])
        self.assertIsNone(member["auto_kick_at"])
        self.assertIsNone(mapping.joined_at)

    async def test_different_user_id_does_not_reuse_previous_profile(self):
        await self.fetch()
        member = await self.fetch(id="different-user", name=None, created_time=None)
        self.assertIsNone(member["name"])
        self.assertIsNone(member["added_at"])
        self.assertIsNone(member["auto_kick_at"])

    async def test_owner_and_non_participating_member_keep_no_exit_schedule(self):
        owner = await self.fetch(role="account-owner")
        self.assertIsNone(owner["auto_kick_at"])
        mapping = await self.mapping()
        mapping.auto_kick_enabled = False
        await self.db.commit()
        member = await self.fetch()
        self.assertFalse(member["auto_kick_enabled"])
        self.assertIsNone(member["auto_kick_at"])

    async def test_team_sync_also_updates_profile_and_repairs_exit_schedule(self):
        await self.fetch()
        mapping = await self.mapping()
        mapping.auto_kick_at = None
        await self.db.commit()
        self.remote.get_members.return_value = {"success": True, "total": 1, "members": [{
            "id": "user-1", "name": "同步后的姓名", "created_time": JOINED_AT,
        }]}
        self.remote.get_account_info = AsyncMock(return_value={"success": True, "accounts": [{
            "account_id": "account-1", "name": "Test Team", "plan_type": "team",
            "subscription_plan": "team", "expires_at": None,
        }]})
        self.remote.get_account_settings = AsyncMock(return_value={"success": True, "data": {}})
        result = await self.service.sync_team_info(1, self.db)
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["member_emails"], [EMAIL])
        self.assertEqual(mapping.member_name, "同步后的姓名")
        self.assertEqual(mapping.auto_kick_at, datetime(2026, 10, 15, 15, 13))


class MemberDateTests(unittest.TestCase):
    def test_timezone_offset_is_converted_to_configured_timezone(self):
        parsed = parse_member_joined_at("2026-10-15T05:13:00Z", pytz.timezone("Asia/Taipei"))
        self.assertEqual(parsed, datetime.fromisoformat(JOINED_AT))

    def test_boolean_is_not_a_timestamp(self):
        with self.assertRaises(ValueError):
            parse_member_joined_at(True, pytz.UTC)
