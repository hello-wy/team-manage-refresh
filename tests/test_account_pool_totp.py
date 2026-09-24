import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app.database import get_db
from app.dependencies.auth import require_admin
from app.main import app
from app.services.account_pool_totp import AccountPoolTotpError, AccountPoolTotpService
from app.utils.totp import generate_totp

NEW_SECRET = "JBSWY3DPEHPK3PXP"


def response(data, status=200):
    return SimpleNamespace(status_code=status, json=Mock(return_value=data))


def info(factor_id="", enabled=False):
    factors = [{"id": factor_id}] if factor_id else []
    return {"mfa_enabled": enabled, "factors": {"totp": factors}}


class AccountPoolTotpServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            cookies={"oai-did": "device-id"},
            get=AsyncMock(), post=AsyncMock(),
        )
        self.authorization = SimpleNamespace(login_entry=AsyncMock(return_value=SimpleNamespace(
            payload={"accounts": [{"credentials": {
                "access_token": "access-token", "email": "member@example.com",
            }}]},
            verified_totp_factor_id="old-id",
        )))
        self.credentials = SimpleNamespace(update_credentials=AsyncMock(return_value={"email": "member@example.com"}))
        self.remote = SimpleNamespace(
            _get_session=AsyncMock(return_value=self.session),
            clear_session=AsyncMock(),
        )
        self.service = AccountPoolTotpService(self.authorization, self.credentials, self.remote)

    async def test_rotates_factor_and_saves_secret_after_remote_confirmation(self):
        self.session.get.side_effect = [
            response(info("old-id", True)), response(info()), response(info("new-id", True)),
        ]
        self.session.post.side_effect = [
            response({"success": True}),
            response({"secret": NEW_SECRET, "session_id": "enroll-session"}),
            response({"success": True}),
        ]

        secret = await self.service.rotate(Mock(), 7)

        self.assertEqual(secret, NEW_SECRET)
        calls = self.session.post.await_args_list
        self.assertTrue(calls[0].args[0].endswith("/mfa/user/disable_in_house"))
        self.assertEqual(calls[0].kwargs["json"], {"factor_id": "old-id"})
        self.assertEqual(calls[1].kwargs["json"], {"factor_type": "totp"})
        self.assertEqual(calls[2].kwargs["json"], {
            "code": generate_totp(NEW_SECRET),
            "factor_type": "totp",
            "session_id": "enroll-session",
        })
        self.credentials.update_credentials.assert_awaited_once_with(
            unittest.mock.ANY, 7, two_factor_secret=NEW_SECRET
        )
        self.remote.clear_session.assert_awaited_once_with("account-pool-login-7")

    async def test_does_not_enroll_when_disable_is_unconfirmed(self):
        self.session.get.side_effect = [response(info("old-id", True)), response(info("old-id", True))]
        self.session.post.return_value = response({"success": True})

        with self.assertRaisesRegex(AccountPoolTotpError, "仍存在"):
            await self.service.rotate(Mock(), 7)

        self.assertEqual(self.session.post.await_count, 1)
        self.credentials.update_credentials.assert_not_awaited()

    async def test_does_not_disable_when_verified_factor_differs(self):
        self.session.get.return_value = response(info("old-id", True))
        self.authorization.login_entry.return_value.verified_totp_factor_id = "other-id"

        with self.assertRaisesRegex(AccountPoolTotpError, "不一致"):
            await self.service.rotate(Mock(), 7)

        self.session.post.assert_not_awaited()
        self.credentials.update_credentials.assert_not_awaited()

    async def test_exposes_new_secret_when_remote_activated_but_save_fails(self):
        self.session.get.side_effect = [
            response(info("old-id", True)), response(info()), response(info("new-id", True)),
        ]
        self.session.post.side_effect = [
            response({"success": True}),
            response({"secret": NEW_SECRET, "session_id": "enroll-session"}),
            response({"success": True}),
        ]
        self.credentials.update_credentials.side_effect = RuntimeError("database unavailable")

        with self.assertRaises(AccountPoolTotpError) as failure:
            await self.service.rotate(Mock(), 7)

        self.assertEqual(failure.exception.new_secret, NEW_SECRET)
        self.assertIn("本地保存失败", str(failure.exception))

    async def test_reports_old_factor_disabled_when_enrollment_fails(self):
        self.session.get.side_effect = [response(info("old-id", True)), response(info())]
        self.session.post.side_effect = [
            response({"success": True}), response({}, status=403),
        ]

        with self.assertRaisesRegex(AccountPoolTotpError, "旧 2FA 已禁用"):
            await self.service.rotate(Mock(), 7)

        self.credentials.update_credentials.assert_not_awaited()


class AccountPoolTotpRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

        async def database():
            yield AsyncMock()

        app.dependency_overrides[get_db] = database

    def tearDown(self):
        app.dependency_overrides.clear()
        self.client.close()

    def test_requires_admin(self):
        self.assertIn(self.client.post("/admin/account-pool/7/rotate-2fa").status_code, (401, 403))

    def test_returns_new_secret_without_cache(self):
        app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        with patch("app.routes.account_pool_totp.totp_service.rotate", new=AsyncMock(return_value=NEW_SECRET)):
            result = self.client.post("/admin/account-pool/7/rotate-2fa")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(result.json()["two_factor_secret"], NEW_SECRET)

    def test_returns_secret_on_partial_failure(self):
        app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        failure = AccountPoolTotpError("本地保存失败", new_secret=NEW_SECRET)
        with patch("app.routes.account_pool_totp.totp_service.rotate", new=AsyncMock(side_effect=failure)):
            result = self.client.post("/admin/account-pool/7/rotate-2fa")

        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()["two_factor_secret"], NEW_SECRET)
