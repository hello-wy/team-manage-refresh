import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import MemberAuthorization, Sub2apiAccountLink, Sub2apiExportRecord, Team, TeamEmailMapping
from app.services.sub2api import Sub2apiConfig, Sub2apiError, Sub2apiService
from app.services.sub2api_cleanup import enqueue_member_cleanup, process_pending_cleanup, track_export
from app.services.sub2api_export_records import sub2api_export_record_service
from app.services.team import TeamService
from app.utils.time_utils import get_now


class CleanupHttpTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, handler, base_url="https://solidapi.top"):
        service = Sub2apiService(lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs))
        with patch.object(service, "_config", new=AsyncMock(return_value=Sub2apiConfig(
                "https://solidapi.top", "test-key"))):
            await service.delete_exported_account(42, "member@example.com", "space-a", base_url, object())

    @staticmethod
    def account(**changes):
        data = {"id": 42, "platform": "openai", "type": "oauth", "credentials": {
            "email": " MEMBER@example.com ", "chatgpt_account_id": "space-a"}}
        data.update(changes)
        return data

    async def test_verified_identity_and_idempotent_not_found(self):
        for get_code, delete_code in ((200, 200), (200, 204), (200, 404), (404, None)):
            calls = []
            def handler(request):
                calls.append(request.method)
                self.assertEqual(request.url.path, "/api/v1/admin/accounts/42")
                if request.method == "GET":
                    return httpx.Response(get_code, json={"code": 0, "data": self.account()})
                return httpx.Response(delete_code, json={"code": 0, "data": {"message": "deleted"}})
            await self.invoke(handler)
            self.assertEqual(calls, ["GET"] if get_code == 404 else ["GET", "DELETE"])

    async def test_wrong_identity_or_failed_lookup_never_deletes(self):
        cases = [self.account(id=43), self.account(platform="claude"), self.account(type="apikey"),
                 self.account(credentials={"email": "other@example.com", "chatgpt_account_id": "space-a"}),
                 self.account(credentials={"email": "member@example.com", "chatgpt_account_id": "space-b"}),
                 self.account(credentials=None)]
        for code, data in [(200, value) for value in cases] + [(401, {}), (500, {})]:
            calls = []
            def handler(request):
                calls.append(request.method)
                return httpx.Response(code, json={"code": 0, "data": data})
            with self.assertRaises(Sub2apiError):
                await self.invoke(handler)
            self.assertEqual(calls, ["GET"])

    async def test_changed_site_and_timeout_keep_task_retryable(self):
        handler = unittest.mock.Mock()
        with self.assertRaisesRegex(Sub2apiError, "地址已变更"):
            await self.invoke(handler, "https://old.example.com")
        handler.assert_not_called()
        def timeout(request):
            raise httpx.ReadTimeout("secret response", request=request)
        with self.assertRaisesRegex(Sub2apiError, "自动重试") as error:
            await self.invoke(timeout)
        self.assertNotIn("secret", str(error.exception))

    async def test_delete_timeout_and_failed_response_are_not_treated_as_success(self):
        for failure in ("timeout", "http", "body"):
            calls = []
            def handler(request):
                calls.append(request.method)
                if request.method == "GET":
                    return httpx.Response(200, json={"code": 0, "data": self.account()})
                if failure == "timeout":
                    raise httpx.ReadTimeout("timeout", request=request)
                return httpx.Response(503 if failure == "http" else 200,
                                      json={"code": 1, "data": {}})
            with self.assertRaises(Sub2apiError):
                await self.invoke(handler)
            self.assertEqual(calls, ["GET", "DELETE"])


class CleanupPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.settings = patch("app.services.sub2api_cleanup.settings_service.get_setting",
                              new=AsyncMock(return_value="https://solidapi.top"))
        self.settings.start()
        async with self.sessions() as db:
            db.add(Team(id=1, email="owner@example.com", account_id="space-a", access_token_encrypted="unused"))
            db.add(TeamEmailMapping(team_id=1, email="member@example.com", status="joined",
                                   upstream_user_id="user-member", replacement_export_pending=True))
            await db.commit()

    async def asyncTearDown(self):
        self.settings.stop()
        await self.engine.dispose()

    async def exported(self, db, account_id=42, space="space-a"):
        return await sub2api_export_record_service.record_success(
            db, email="member@example.com", team_space_id=space,
            sub2api_account_id=account_id, team_id=1 if space == "space-a" else None)

    async def links(self, db):
        return (await db.execute(select(Sub2apiAccountLink).order_by(Sub2apiAccountLink.id))).scalars().all()

    async def test_removal_tracks_duplicate_exports_but_not_other_team(self):
        async with self.sessions() as db:
            await self.exported(db)
            await self.exported(db, 43)
            await self.exported(db, 44, "space-b")
            await db.commit()
            mapping = await TeamService().mark_team_email_mapping_removed(1, "member@example.com", db)
            await db.commit()
            self.assertFalse(mapping.replacement_export_pending)
            self.assertEqual([(r.sub2api_account_id, r.status) for r in await self.links(db)],
                             [(42, "pending"), (43, "pending"), (44, "active")])

    async def test_legacy_export_id_is_enqueued_with_removal_transaction_and_rollback(self):
        async with self.sessions() as db:
            db.add(MemberAuthorization(team_id=1, email="member@example.com", account_id="space-a",
                                       sub2api_account_id=42))
            await db.commit()
            await TeamService().mark_team_email_mapping_removed(1, "member@example.com", db)
            await db.rollback()
            self.assertEqual(await self.links(db), [])
            await TeamService().mark_team_email_mapping_removed(1, "member@example.com", db)
            await db.commit()
            self.assertEqual((await self.links(db))[0].status, "pending")

    async def test_owner_and_unexported_member_do_not_create_cleanup(self):
        async with self.sessions() as db:
            team = await db.get(Team, 1)
            await track_export(db, team.email, team.account_id, 45)
            self.assertEqual(await enqueue_member_cleanup(db, team, team.email), 0)
            self.assertEqual(await enqueue_member_cleanup(db, team, "member@example.com"), 0)
            await db.commit()
            service = SimpleNamespace(delete_exported_account=AsyncMock())
            self.assertEqual((await process_pending_cleanup(db, service))["scanned"], 0)
            service.delete_exported_account.assert_not_awaited()

    async def test_only_third_missing_sync_enqueues_and_history_is_not_retroactive(self):
        async with self.sessions() as db:
            await self.exported(db)
            db.add(TeamEmailMapping(team_id=1, email="old@example.com", status="removed"))
            await track_export(db, "old@example.com", "space-a", 45)
            await db.commit()
            teams = TeamService()
            for attempt in range(3):
                await teams._reconcile_team_email_mappings(1, set(), set(), db)
                await db.commit()
                rows = await self.links(db)
                self.assertEqual(rows[0].status, "pending" if attempt == 2 else "active")
                self.assertEqual(rows[1].status, "active")

    async def test_late_export_after_removal_is_immediately_queued(self):
        async with self.sessions() as db:
            await TeamService().mark_team_email_mapping_removed(1, "member@example.com", db)
            await self.exported(db)
            await db.commit()
            self.assertEqual((await self.links(db))[0].status, "pending")

    async def test_retry_survives_new_session_and_does_not_clear_new_export_marker(self):
        async with self.sessions() as db:
            await self.exported(db)
            await self.exported(db, 43)
            await TeamService().mark_team_email_mapping_removed(1, "member@example.com", db)
            await db.commit()
            service = SimpleNamespace(delete_exported_account=AsyncMock(
                side_effect=[Sub2apiError("暂时失败"), None]))
            self.assertEqual(await process_pending_cleanup(db, service), {"scanned": 2, "deleted": 1, "failed": 1})
        async with self.sessions() as db:
            first, second = await self.links(db)
            self.assertEqual((first.status, first.attempts, second.status), ("pending", 1, "deleted"))
            first.next_attempt_at = get_now() - timedelta(seconds=1)
            mapping = (await db.execute(select(TeamEmailMapping))).scalar_one()
            mapping.status = "joined"
            await self.exported(db, 99)
            db.add(MemberAuthorization(team_id=1, email="member@example.com", account_id="space-a",
                                       sub2api_account_id=99, sub2api_exported_at=get_now()))
            await db.commit()
            service = SimpleNamespace(delete_exported_account=AsyncMock())
            self.assertEqual((await process_pending_cleanup(db, service))["deleted"], 1)
            self.assertEqual((await db.execute(select(Sub2apiExportRecord))).scalar_one().sub2api_account_id, 99)
            self.assertEqual((await db.execute(select(MemberAuthorization))).scalar_one().sub2api_account_id, 99)
            self.assertEqual(first.attempts, 2)

    async def test_success_clears_current_marker_but_preserves_export_history(self):
        async with self.sessions() as db:
            await self.exported(db)
            db.add(MemberAuthorization(team_id=1, email="member@example.com", account_id="space-a",
                                       sub2api_account_id=42, sub2api_exported_at=get_now()))
            await TeamService().mark_team_email_mapping_removed(1, "member@example.com", db)
            await db.commit()
            await process_pending_cleanup(db, SimpleNamespace(delete_exported_account=AsyncMock()))
            auth = (await db.execute(select(MemberAuthorization))).scalar_one()
            record = (await db.execute(select(Sub2apiExportRecord))).scalar_one()
            self.assertIsNone(auth.sub2api_account_id)
            self.assertIsNone(auth.sub2api_exported_at)
            self.assertIsNone(record.sub2api_account_id)
            self.assertEqual(record.export_count, 1)
            self.assertIsNotNone(record.last_exported_at)

    async def test_confirmed_delete_enqueues_but_failed_delete_does_not(self):
        async with self.sessions() as db:
            await self.exported(db)
            await db.commit()
            teams = TeamService()
            teams.ensure_access_token = AsyncMock(return_value="test-token")
            teams._delete_remote_member = AsyncMock(return_value={"success": False, "error": "not confirmed"})
            teams.sync_team_info = AsyncMock()
            teams._release_member_seat = AsyncMock()
            teams._reset_error_status = AsyncMock()
            result = await teams._delete_team_member_locked(1, "user-member", db, "member@example.com")
            self.assertFalse(result["success"])
            self.assertEqual((await self.links(db))[0].status, "active")
            teams._delete_remote_member.return_value = {"success": True}
            result = await teams._delete_team_member_locked(1, "user-member", db)
            self.assertTrue(result["success"])
            self.assertEqual((await self.links(db))[0].status, "pending")
