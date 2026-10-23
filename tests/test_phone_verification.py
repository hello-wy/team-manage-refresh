import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from app.services.phone_verification import (
    PhoneVerificationError,
    complete_herosms_phone_verification,
)


class PhoneVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_submits_received_code_and_completes_rental(self):
        request = SimpleNamespace(db_session=object())
        with (
            patch("app.services.phone_verification.settings_service.get_setting", new=AsyncMock(
                side_effect=["encrypted-key", "https://hero-sms.com/stubs/handler_api.php", "44", "0.05"]
            )),
            patch("app.services.phone_verification.encryption_service.decrypt_token", return_value="api-key"),
            patch("app.services.phone_verification._rent_number", new=AsyncMock(
                return_value=("activation-1", "+447700900123")
            )) as rent,
            patch("app.services.phone_verification._send_code", new=AsyncMock()) as send,
            patch("app.services.phone_verification._wait_for_code", new=AsyncMock(return_value="654321")) as wait,
            patch("app.services.phone_verification._submit_code", new=AsyncMock(
                return_value="https://auth.openai.com/workspace"
            )) as submit,
            patch("app.services.phone_verification._finish_activation", new=AsyncMock()) as finish,
        ):
            result = await complete_herosms_phone_verification(
                request, object(), "device", "sentinel", "https://auth.openai.com/add-phone"
            )

        self.assertEqual(result["phone_number"], "+447700900123")
        self.assertEqual(result["continue_url"], "https://auth.openai.com/workspace")
        rent.assert_awaited_once_with("api-key", "https://hero-sms.com/stubs/handler_api.php", "44", "0.05")
        send.assert_awaited_once()
        wait.assert_awaited_once_with("api-key", "https://hero-sms.com/stubs/handler_api.php", "activation-1")
        submit.assert_awaited_once_with(ANY, "device", "sentinel", "654321")
        finish.assert_awaited_once_with("api-key", "https://hero-sms.com/stubs/handler_api.php", "activation-1", "6")

    async def test_validation_failure_releases_rental_and_surfaces_failure(self):
        request = SimpleNamespace(db_session=object())
        with (
            patch("app.services.phone_verification.settings_service.get_setting", new=AsyncMock(
                side_effect=["encrypted-key", "", "38", "0.06"]
            )),
            patch("app.services.phone_verification.encryption_service.decrypt_token", return_value="api-key"),
            patch("app.services.phone_verification._rent_number", new=AsyncMock(
                return_value=("activation-2", "+233123456789")
            )),
            patch("app.services.phone_verification._send_code", new=AsyncMock()),
            patch("app.services.phone_verification._wait_for_code", new=AsyncMock(return_value="000000")),
            patch("app.services.phone_verification._submit_code", new=AsyncMock(
                side_effect=PhoneVerificationError("OpenAI 拒绝了手机验证码")
            )),
            patch("app.services.phone_verification._finish_activation", new=AsyncMock()) as finish,
        ):
            with self.assertRaisesRegex(PhoneVerificationError, "拒绝了手机验证码"):
                await complete_herosms_phone_verification(
                    request, object(), "device", "sentinel", "https://auth.openai.com/add-phone"
                )

        finish.assert_awaited_once_with("api-key", "https://hero-sms.com/stubs/handler_api.php", "activation-2", "8")
