"""Exercise real pagination and persistence across independently served batches."""
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Team, TeamEmailMapping
from app.services.chatgpt import ChatGPTService
from app.services.member_pagination import MEMBER_BATCH_PAGES
from app.services.team import TeamService

JOINED_AT = "2026-10-15T13:13:00"
TOTAL_MEMBERS = MEMBER_BATCH_PAGES + 2


class MemberBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.db.add(Team(id=1, email="owner@example.com", account_id="account-1",
                         access_token_encrypted="encrypted", max_members=TOTAL_MEMBERS,
                         member_auto_kick_hours=2))
        await self.db.commit()
        self.offsets = []
        self.total = TOTAL_MEMBERS
        self.fail_offset = None
        self.remote = ChatGPTService()
        self.remote._make_request = AsyncMock(side_effect=self.page)
        self.remote.get_invites = AsyncMock(return_value={"success": True, "items": []})
        self.remote.get_seat_capacity = AsyncMock(return_value={"success": True, "data": {
            "seat_capacity": [{"type": "default", "paid": TOTAL_MEMBERS, "available": 0},
                              {"type": "premium", "paid": 0, "available": 0}],
        }})
        self.service = TeamService()
        self.service.chatgpt_service = self.remote
        self.service.ensure_access_token = AsyncMock(return_value="test-token")

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def page(self, method, url, headers, **kwargs):
        offset = int(parse_qs(urlparse(url).query)["offset"][0])
        self.offsets.append(offset)
        if offset == self.fail_offset:
            return {"success": False, "error": "upstream timeout", "status_code": 504}
        items = [] if offset >= self.total else [{
            "id": f"user-{offset}", "email": f"member{offset}@example.com",
            "name": f"Member {offset}", "role": "standard-user", "seat_type": "default",
            "created_time": JOINED_AT,
        }]
        return {"success": True, "data": {"items": items, "total": self.total}}

    async def sync(self, cursor=None):
        return await self.service.get_team_members(1, self.db, pagination={"cursor": cursor})

    async def test_each_request_reads_ten_pages_and_resumes_without_skipping(self):
        first = await self.sync()
        self.assertTrue(first["success"], first.get("error"))
        self.assertEqual(self.offsets, list(range(MEMBER_BATCH_PAGES)))
        self.assertEqual(first["pagination"]["loaded"], MEMBER_BATCH_PAGES)
        self.assertTrue(first["pagination"]["has_more"])
        self.assertEqual(first["joined_members"], TOTAL_MEMBERS)
        self.assertFalse(first["seat_balance"]["success"])
        final = await self.sync(first["pagination"]["next_cursor"])
        self.assertTrue(final["success"], final.get("error"))
        self.assertEqual(self.offsets, list(range(TOTAL_MEMBERS)))
        self.assertEqual(len(final["members"]), TOTAL_MEMBERS)
        self.assertFalse(final["pagination"]["has_more"])
        self.assertIsNone(final["pagination"]["next_cursor"])
        self.assertTrue(final["seat_balance"]["success"])
        mapping = (await self.db.execute(select(TeamEmailMapping).where(
            TeamEmailMapping.email == "member11@example.com",
        ))).scalar_one()
        self.assertEqual(mapping.member_name, "Member 11")
        self.assertEqual(mapping.auto_kick_at.isoformat(), "2026-10-15T15:13:00")

    async def test_partial_sync_does_not_mark_unseen_members_missing(self):
        later = TeamEmailMapping(team_id=1, email="member11@example.com", status="joined",
                                 upstream_user_id="user-11", missing_sync_count=2)
        absent = TeamEmailMapping(team_id=1, email="absent@example.com", status="joined")
        self.db.add_all([later, absent])
        await self.db.commit()
        first = await self.sync()
        self.assertEqual(later.status, "joined")
        self.assertEqual(later.missing_sync_count, 2)
        self.assertEqual(absent.missing_sync_count, 0)
        await self.sync(first["pagination"]["next_cursor"])
        self.assertEqual(later.missing_sync_count, 0)
        self.assertEqual(absent.missing_sync_count, 1)

    async def test_reopening_starts_at_first_page_and_refreshes_profile(self):
        self.total = 1
        first = await self.sync()
        self.assertEqual(first["members"][0]["name"], "Member 0")
        original_page = self.page

        async def renamed(*args, **kwargs):
            result = await original_page(*args, **kwargs)
            return {**result, "data": {**result["data"], "items": [
                {**member, "name": "Updated name"} for member in result["data"]["items"]
            ]}}

        self.remote._make_request.side_effect = renamed
        reopened = await self.sync()
        self.assertEqual(self.offsets, [0, 0])
        self.assertEqual(reopened["members"][0]["name"], "Updated name")

    async def test_internal_full_snapshot_still_fetches_all_batches(self):
        result = await self.service.get_team_members(1, self.db)
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(len(result["members"]), TOTAL_MEMBERS)
        self.assertEqual(self.offsets, list(range(TOTAL_MEMBERS)))
        self.assertNotIn("pagination", result)

    async def test_failure_in_later_batch_is_exposed_without_reconciling_absence(self):
        first = await self.sync()
        self.fail_offset = MEMBER_BATCH_PAGES
        failed = await self.sync(first["pagination"]["next_cursor"])
        self.assertFalse(failed["success"])
        self.assertIn("upstream timeout", failed["error"])
        mappings = (await self.db.execute(select(TeamEmailMapping))).scalars().all()
        self.assertEqual(len(mappings), MEMBER_BATCH_PAGES)
        self.assertTrue(all(mapping.missing_sync_count == 0 for mapping in mappings))

    async def test_cursor_cannot_be_tampered_with_or_used_for_another_workspace(self):
        first = await self.sync()
        cursor = first["pagination"]["next_cursor"]
        self.remote._make_request.reset_mock()
        tampered = await self.sync(cursor[:10] + ("a" if cursor[10] != "a" else "b") + cursor[11:])
        self.assertFalse(tampered["success"])
        team = await self.db.get(Team, 1)
        team.account_id = "another-account"
        await self.db.commit()
        mismatched = await self.sync(cursor)
        self.assertFalse(mismatched["success"])
        self.assertIn("不匹配", mismatched["error"])
        self.remote._make_request.assert_not_awaited()


class MemberPaginationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_page_with_members_remaining_fails_without_looping(self):
        remote = ChatGPTService()
        remote._make_request = AsyncMock(return_value={
            "success": True, "data": {"items": [], "total": 1},
        })
        result = await remote.get_members("token", "account", None)
        self.assertFalse(result["success"])
        self.assertIn("空页", result["error"])
        self.assertEqual(remote._make_request.await_count, 1)

    async def test_invalid_response_and_total_are_explicit_failures(self):
        remote = ChatGPTService()
        for data in ({}, {"items": [None], "total": 1}, {"items": [], "total": -1}):
            with self.subTest(data=data):
                remote._make_request = AsyncMock(return_value={"success": True, "data": data})
                self.assertFalse((await remote.get_members_batch("token", "account", None))["success"])


class MemberSyncRouteTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app.main import app
        self.app = app
        self.client = TestClient(app)
        self.previous_overrides = app.dependency_overrides.copy()

    def tearDown(self):
        self.app.dependency_overrides = self.previous_overrides
        self.client.close()

    def test_sync_requires_admin(self):
        response = self.client.post("/admin/teams/1/members/sync", json={})
        self.assertIn(response.status_code, (401, 403))

    def test_cursor_is_forwarded_and_response_is_never_cached(self):
        from app.database import get_db
        from app.dependencies.auth import require_admin
        from app.routes import admin
        self.app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        self.app.dependency_overrides[get_db] = lambda: None
        for success in (True, False):
            with self.subTest(success=success):
                result = {"success": success, "members": [], "error": None if success else "failed"}
                with patch.object(admin.team_service, "get_team_members", new=AsyncMock(return_value=result)) as fetch:
                    response = self.client.post("/admin/teams/1/members/sync", json={"cursor": "next"})
                fetch.assert_awaited_once_with(1, None, pagination={"cursor": "next"})
                self.assertEqual(response.status_code, 200 if success else 400)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.json(), result)
