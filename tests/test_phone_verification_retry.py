import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import httpx

from app.services import phone_verification as phone


class PhoneNumberReplacementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.progress = AsyncMock()
        self.request = SimpleNamespace(db_session=object(), on_progress=self.progress)
        mocks = {
            "settings_service.get_setting": AsyncMock(side_effect=["encrypted", "", "63", "0.06"]),
            "encryption_service.decrypt_token": lambda _: "api-key",
            "_rent_number": AsyncMock(return_value=("rental-1", "+420111111111")),
            "_send_code": AsyncMock(),
            "_wait_for_code": AsyncMock(return_value="123456"),
            "_submit_code": AsyncMock(return_value="https://auth.openai.com/workspace"),
            "_finish_activation": AsyncMock(),
        }
        self.mocks = {name: stack.enter_context(patch(f"{phone.__name__}.{name}", new=mock))
                      for name, mock in mocks.items()}

    async def run_verification(self):
        return await phone.complete_herosms_phone_verification(
            self.request, object(), "device", "sentinel", "https://auth.openai.com/add-phone",
        )

    async def test_repeated_timeouts_cancel_before_next_rental_until_code_arrives(self):
        timeout_count = 12
        events = []

        async def rent(*args):
            index = sum(event.startswith("rent") for event in events)
            events.append(f"rent-{index}")
            return f"rental-{index}", f"+42011111{index:04}"

        async def finish(_key, _endpoint, activation_id, status):
            events.append(f"finish-{activation_id}-{status}")

        self.mocks["_rent_number"].side_effect = rent
        self.mocks["_finish_activation"].side_effect = finish
        self.mocks["_wait_for_code"].side_effect = [
            *[phone.SmsCodeTimeout("短信超时") for _ in range(timeout_count)], "654321",
        ]
        result = await self.run_verification()
        expected = []
        for index in range(timeout_count):
            expected.extend([f"rent-{index}", f"finish-rental-{index}-8"])
        expected.extend([f"rent-{timeout_count}", f"finish-rental-{timeout_count}-6"])
        self.assertEqual(events, expected)
        self.assertEqual(result["phone_number"], f"+42011111{timeout_count:04}")
        self.assertEqual(self.mocks["_send_code"].await_count, timeout_count + 1)
        for args in self.mocks["_rent_number"].await_args_list:
            self.assertEqual(args, call("api-key", phone.HEROSMS_DEFAULT_ENDPOINT, "63", "0.06"))
        self.mocks["_submit_code"].assert_awaited_once()
        self.assertEqual(self.mocks["_submit_code"].await_args.args[-1], "654321")
        stages = [args.args[0] for args in self.progress.await_args_list]
        self.assertEqual(stages, ["rent_number", "send_code", "wait_code", "cancel_number"]
                         * timeout_count + ["rent_number", "send_code", "wait_code", "verify_code", "finish_rental"])

    async def test_cancel_failure_stops_without_renting_another_number(self):
        self.mocks["_wait_for_code"].side_effect = phone.SmsCodeTimeout("短信超时")
        self.mocks["_finish_activation"].side_effect = phone.PhoneVerificationError("未确认取消")
        with self.assertRaisesRegex(phone.PhoneVerificationError, "短信超时.*取消租号失败.*未确认取消"):
            await self.run_verification()
        self.mocks["_rent_number"].assert_awaited_once()
        self.mocks["_finish_activation"].assert_awaited_once()
        self.mocks["_submit_code"].assert_not_awaited()

    async def test_transport_timeout_does_not_trigger_number_replacement(self):
        self.mocks["_wait_for_code"].side_effect = httpx.ReadTimeout("network timeout")
        with self.assertRaises(httpx.ReadTimeout):
            await self.run_verification()
        self.mocks["_rent_number"].assert_awaited_once()
        self.mocks["_finish_activation"].assert_awaited_once_with(
            "api-key", phone.HEROSMS_DEFAULT_ENDPOINT, "rental-1", "8",
        )
        self.assertNotIn(call("cancel_number"), self.progress.await_args_list)

    async def test_second_number_send_failure_releases_it_and_surfaces_error(self):
        self.mocks["_rent_number"].side_effect = [("first", "+420111111111"), ("second", "+420222222222")]
        self.mocks["_wait_for_code"].side_effect = phone.SmsCodeTimeout("短信超时")
        self.mocks["_send_code"].side_effect = [None, phone.PhoneVerificationError("发送失败")]
        with self.assertRaisesRegex(phone.PhoneVerificationError, "发送失败"):
            await self.run_verification()
        self.assertEqual([args.args[-2:] for args in self.mocks["_finish_activation"].await_args_list],
                         [("first", "8"), ("second", "8")])
        self.assertEqual(self.mocks["_rent_number"].await_count, 2)

    async def test_disconnect_during_timeout_notification_still_releases_number(self):
        async def report(stage):
            if stage == "cancel_number":
                raise asyncio.CancelledError()

        self.progress.side_effect = report
        self.mocks["_wait_for_code"].side_effect = phone.SmsCodeTimeout("短信超时")
        with self.assertRaises(asyncio.CancelledError):
            await self.run_verification()
        self.mocks["_finish_activation"].assert_awaited_once()
        self.mocks["_rent_number"].assert_awaited_once()

    async def test_disconnect_during_cancellation_awaits_confirmation_and_never_rerents(self):
        releasing, confirm = asyncio.Event(), asyncio.Event()

        async def finish(*args):
            releasing.set()
            await confirm.wait()

        self.mocks["_finish_activation"].side_effect = finish
        self.mocks["_wait_for_code"].side_effect = phone.SmsCodeTimeout("短信超时")
        task = asyncio.create_task(self.run_verification())
        await releasing.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        confirm.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.mocks["_rent_number"].assert_awaited_once()
        self.mocks["_finish_activation"].assert_awaited_once()


class SmsPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_deadline_has_distinct_retryable_exception(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, text="STATUS_WAIT_CODE")

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with (patch.object(phone.httpx, "AsyncClient", return_value=client),
              patch.object(phone.asyncio, "sleep", new=AsyncMock()) as sleep):
            with self.assertRaises(phone.SmsCodeTimeout):
                await phone._wait_for_code("api-key", phone.HEROSMS_DEFAULT_ENDPOINT, "rental-1")
        self.assertEqual(len(requests), phone.SMS_WAIT_SECONDS // phone.SMS_POLL_SECONDS)
        self.assertEqual(sleep.await_count, len(requests))

    async def test_provider_rejection_is_not_retryable_wait_timeout(self):
        for status in ("STATUS_CANCEL", "BAD_KEY", "NO_ACTIVATION"):
            with self.subTest(status=status):
                client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, text=status),
                ))
                with patch.object(phone.httpx, "AsyncClient", return_value=client):
                    with self.assertRaises(phone.PhoneVerificationError) as error:
                        await phone._wait_for_code("api-key", phone.HEROSMS_DEFAULT_ENDPOINT, "rental-1")
                self.assertNotIsInstance(error.exception, phone.SmsCodeTimeout)

    async def test_cancellation_requires_provider_confirmation(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="BAD_STATUS"),
        ))
        with patch.object(phone.httpx, "AsyncClient", return_value=client):
            with self.assertRaisesRegex(phone.PhoneVerificationError, "未确认租号结束状态"):
                await phone._finish_activation("api-key", phone.HEROSMS_DEFAULT_ENDPOINT, "rental-1", "8")

    async def test_waiting_emits_heartbeat_after_each_pending_poll(self):
        statuses = iter(["STATUS_WAIT_CODE", "STATUS_WAIT_RETRY:1", "STATUS_OK:654321"])
        progress = AsyncMock()

        def respond(request):
            return httpx.Response(200, text=next(statuses))

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with (patch.object(phone.httpx, "AsyncClient", return_value=client),
              patch.object(phone.asyncio, "sleep", new=AsyncMock())):
            result = await phone._wait_for_code(
                "api-key", phone.HEROSMS_DEFAULT_ENDPOINT, "rental-1", progress,
            )
        self.assertEqual(result, "654321")
        self.assertEqual(progress.await_args_list, [call("wait_code"), call("wait_code")])
