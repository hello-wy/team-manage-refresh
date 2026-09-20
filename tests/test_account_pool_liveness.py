import unittest
import json
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry
from app.models import Setting
from app.services.account_pool import AccountPoolService
from app.services.account_pool_credentials import AccountPoolCredentialService
from app.services.account_pool_liveness import AccountPoolLivenessService
from app.services.encryption import encryption_service
from app.services.openai_automatic_login import OpenAIAutomaticLoginError


class AccountPoolLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.credentials = AccountPoolCredentialService(encryption_service)
        self.login = Mock(verify_credentials=AsyncMock())
        self.auth = Mock(create_oauth_authorize_url=Mock(return_value={
            "authorize_url": "https://auth.openai.com/oauth/authorize",
            "state": "test-state", "code_verifier": "test-verifier",
        }))
        self.service = AccountPoolLivenessService(self.credentials, self.login, self.auth)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def add_account(self, content):
        async with self.sessions() as session:
            await self.credentials.add_accounts(session, content=content)
            return (await session.scalars(select(AccountPoolEntry.id))).first()

    async def read_account(self, entry_id):
        async with self.sessions() as session:
            return await session.get(AccountPoolEntry, entry_id)

    async def test_success_is_persisted_and_visible_in_listing(self):
        entry_id = await self.add_account(
            "member@example.com----password----JBSWY3DPEHPK3PXP"
        )
        async with self.sessions() as session:
            self.assertEqual(await self.service.check_entry(session, entry_id), "alive")
            listing = await AccountPoolService(self.credentials).list_entries(session)

        entry = await self.read_account(entry_id)
        self.assertEqual(entry.liveness_status, "alive")
        self.assertIsInstance(entry.liveness_checked_at, datetime)
        self.assertEqual(listing["entries"][0]["liveness_status"], "alive")
        self.assertEqual(self.login.verify_credentials.await_args.args[0].email, entry.email)

    async def test_invalid_credentials_are_distinct_from_network_failure(self):
        entry_id = await self.add_account("member@example.com----password----JBSWY3DPEHPK3PXP")
        self.login.verify_credentials.side_effect = OpenAIAutomaticLoginError(
            "OpenAI: invalid_username_or_password，HTTP 401"
        )
        async with self.sessions() as session:
            self.assertEqual(await self.service.check_entry(session, entry_id), "invalid")

        self.login.verify_credentials.side_effect = ConnectionError("network unavailable")
        with self.assertLogs("app.services.account_pool_liveness", level="ERROR"):
            async with self.sessions() as session:
                self.assertEqual(await self.service.check_entry(session, entry_id), "error")
        self.assertEqual((await self.read_account(entry_id)).liveness_status, "error")

    async def test_deactivated_account_is_invalid(self):
        entry_id = await self.add_account("member@example.com----password----JBSWY3DPEHPK3PXP")
        self.login.verify_credentials.side_effect = OpenAIAutomaticLoginError(
            "2FA 验证失败（HTTP 403，OpenAI: account_deactivated）"
        )
        async with self.sessions() as session:
            self.assertEqual(await self.service.check_entry(session, entry_id), "invalid")
        self.assertEqual((await self.read_account(entry_id)).liveness_status, "invalid")

    async def test_missing_password_and_deleted_account_are_not_logged_in(self):
        entry_id = await self.add_account("member@example.com")
        async with self.sessions() as session:
            self.assertEqual(await self.service.check_entry(session, entry_id), "missing")
            await self.credentials.delete_entry(session, entry_id)
            self.assertIsNone(await self.service.check_entry(session, entry_id))
        self.login.verify_credentials.assert_not_awaited()

    async def test_credential_change_clears_previous_result(self):
        entry_id = await self.add_account("member@example.com----password----JBSWY3DPEHPK3PXP")
        async with self.sessions() as session:
            await self.service.check_entry(session, entry_id)
            await self.credentials.update_credentials(session, entry_id, password="new-password")
        entry = await self.read_account(entry_id)
        self.assertIsNone(entry.liveness_status)
        self.assertIsNone(entry.liveness_checked_at)

    async def test_check_all_commits_each_account(self):
        await self.add_account("one@example.com----password----JBSWY3DPEHPK3PXP")
        await self.add_account("two@example.com")
        counts = await self.service.check_all(self.sessions)
        self.assertEqual(counts, {"alive": 1, "invalid": 0, "error": 0, "missing": 1})

    async def test_manual_liveness_route_returns_counts(self):
        from app.routes.admin import run_account_pool_liveness

        counts = {"alive": 2, "invalid": 1, "error": 0, "missing": 3}
        with patch("app.main.scheduled_account_pool_liveness", new=AsyncMock(return_value=counts)):
            response = await run_account_pool_liveness({"username": "admin"})

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["counts"], counts)
        self.assertIn("正常 2", payload["message"])

    async def test_credential_changed_during_check_does_not_save_stale_result(self):
        entry_id = await self.add_account("member@example.com----password----JBSWY3DPEHPK3PXP")

        async def edit_during_check(request):
            await self.credentials.update_credentials(
                request.db_session, entry_id, password="new-password"
            )

        self.login.verify_credentials.side_effect = edit_during_check
        async with self.sessions() as session:
            self.assertIsNone(await self.service.check_entry(session, entry_id))
        entry = await self.read_account(entry_id)
        self.assertIsNone(entry.liveness_status)
        self.assertIsNone(entry.liveness_checked_at)


class AccountPoolCronTests(unittest.TestCase):
    def test_default_runs_daily_at_two_thirty_in_configured_timezone(self):
        from app import main

        trigger = main.account_pool_liveness_trigger(main.DEFAULT_ACCOUNT_POOL_LIVENESS_CRON)
        next_run = trigger.get_next_fire_time(None, datetime(2026, 9, 20, tzinfo=trigger.timezone))
        self.assertEqual((next_run.hour, next_run.minute), (2, 30))
        self.assertEqual(str(next_run.tzinfo), main.settings.timezone)

    def test_invalid_cron_does_not_change_job(self):
        from app import main

        with patch.object(main, "scheduler") as scheduler:
            with self.assertRaises(ValueError):
                main.configure_account_pool_liveness_job("not a cron expression")
            scheduler.add_job.assert_not_called()

    def test_saving_cron_reschedules_existing_job(self):
        from app import main

        with patch.object(main, "scheduler") as scheduler:
            scheduler.get_job.return_value = object()
            scheduler.running = True
            main.configure_account_pool_liveness_job("0 3 * * *")
            scheduler.reschedule_job.assert_called_once()
            self.assertEqual(scheduler.reschedule_job.call_args.args[0], "account_pool_liveness")


class AccountPoolCronSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        from app.services.settings import settings_service

        settings_service.clear_cache()
        await self.engine.dispose()

    async def test_invalid_expression_is_rejected_without_saving(self):
        from app.routes.admin import (
            AccountPoolLivenessSettingsRequest, update_account_pool_liveness_settings,
        )

        async with self.sessions() as session:
            response = await update_account_pool_liveness_settings(
                AccountPoolLivenessSettingsRequest(cron="wrong"), session, {}
            )
            stored = (await session.scalars(select(Setting))).all()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(stored, [])

    async def test_valid_expression_is_saved_and_scheduled(self):
        from app import main
        from app.routes.admin import (
            AccountPoolLivenessSettingsRequest, update_account_pool_liveness_settings,
        )

        with patch.object(main, "scheduler") as scheduler:
            scheduler.get_job.return_value = None
            scheduler.running = True
            async with self.sessions() as session:
                response = await update_account_pool_liveness_settings(
                    AccountPoolLivenessSettingsRequest(cron=" 0 3 * * * "), session, {}
                )
                stored = (await session.scalars(select(Setting))).one()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["cron"], "0 3 * * *")
        self.assertEqual(stored.value, "0 3 * * *")
        self.assertEqual(scheduler.add_job.call_args.kwargs["id"], "account_pool_liveness")

    async def test_settings_and_pool_pages_render_liveness_controls(self):
        from app.routes.admin import account_pool_page, settings_page

        async with self.sessions() as session:
            await AccountPoolCredentialService(encryption_service).add_accounts(
                session, content="member@example.com----password----JBSWY3DPEHPK3PXP"
            )
            settings_request = Request({
                "type": "http", "method": "GET", "path": "/admin/settings",
                "headers": [], "query_string": b"", "server": ("test", 80),
            })
            pool_request = Request({
                "type": "http", "method": "GET", "path": "/admin/account-pool",
                "headers": [], "query_string": b"", "server": ("test", 80),
            })
            settings_response = await settings_page(settings_request, session, {"username": "admin"})
            pool_response = await account_pool_page(
                pool_request, 1, 20, "", "", session, {"username": "admin"}
            )
        self.assertIn(b'accountPoolLivenessCron', settings_response.body)
        self.assertIn(b'30 2 * * *', settings_response.body)
        self.assertIn('验活状态'.encode(), pool_response.body)
        self.assertIn('未检测'.encode(), pool_response.body)
        self.assertIn('立即验活'.encode(), pool_response.body)
        self.assertIn('列设置'.encode(), pool_response.body)
        self.assertIn(b'accountPoolColumnToggleDropdown', pool_response.body)
