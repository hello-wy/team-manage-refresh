import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import StreamingResponse

from app.routes import account_pool_phone as phone
from app.services.operation_progress import stream_operation

EVENT_TIMEOUT_SECONDS = 1


class OperationProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_arrives_before_operation_finishes(self):
        proceed = asyncio.Event()

        async def operation(report):
            await report("wait_code")
            await proceed.wait()
            return {"type": "result", "success": True}

        stream = stream_operation(operation)
        first = await asyncio.wait_for(anext(stream), EVENT_TIMEOUT_SECONDS)
        self.assertEqual(json.loads(first), {"type": "progress", "stage": "wait_code"})
        proceed.set()
        self.assertEqual(json.loads(await anext(stream))["type"], "result")
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

    async def test_stream_disconnect_cancels_and_awaits_operation(self):
        cleaned_up = asyncio.Event()

        async def operation(report):
            try:
                await report("wait_code")
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

        stream = stream_operation(operation)
        await anext(stream)
        await stream.aclose()
        self.assertTrue(cleaned_up.is_set())

    async def test_unhandled_worker_failure_is_not_silenced(self):
        async def operation(report):
            raise RuntimeError("worker failed")

        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            async for _ in stream_operation(operation):
                self.fail("must not report success")

    async def test_gzip_does_not_buffer_progress_until_operation_finishes(self):
        proceed = asyncio.Event()
        first_body = asyncio.Event()
        messages = []

        async def operation(report):
            await report("wait_code")
            await proceed.wait()
            return {"type": "result", "success": True}

        async def app(scope, receive, send):
            response = StreamingResponse(stream_operation(operation), headers={
                "Content-Encoding": "identity",
            })
            await response(scope, receive, send)

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body":
                first_body.set()

        scope = {"type": "http", "method": "POST", "path": "/", "asgi": {"spec_version": "2.4"},
                 "headers": [(b"accept-encoding", b"gzip")]}
        task = asyncio.create_task(GZipMiddleware(app)(scope, AsyncMock(), send))
        try:
            await asyncio.wait_for(first_body.wait(), EVENT_TIMEOUT_SECONDS)
            self.assertIn(b'"wait_code"', messages[1]["body"])
        finally:
            proceed.set()
            await task


class PhoneProgressRouteTests(unittest.IsolatedAsyncioTestCase):
    def database(self):
        return SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
            deleted_at=None, password_encrypted="password", two_factor_secret_encrypted="totp",
            workspace_id="workspace",
        )), rollback=AsyncMock())

    async def test_real_milestones_precede_saved_success(self):
        db = self.database()

        @asynccontextmanager
        async def session_factory():
            yield db

        async def login(*args, **kwargs):
            await kwargs["on_progress"]("wait_code")
            return SimpleNamespace(phone_number="+233123456789", phone_rental_error="")

        with (
            patch.object(phone, "AsyncSessionLocal", session_factory),
            patch.object(phone.account_pool_authorization_service, "login_entry", new=login),
            patch.object(phone.account_pool_authorization_service, "save_result", new=AsyncMock(return_value=True)),
        ):
            response = await phone.add_phone_to_pool_account(75, current_user={})
            events = [json.loads(chunk) async for chunk in response.body_iterator]
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertEqual(response.headers["content-encoding"], "identity")
        self.assertEqual([event.get("stage") for event in events[:-1]], ["login", "wait_code", "save"])
        self.assertEqual(events[-1], {"type": "result", "success": True, "phone": "+233****789"})

    async def test_upstream_failure_is_terminal_and_hides_key(self):
        db = self.database()
        request = httpx.Request("GET", "https://hero-sms.com/stubs/handler_api.php?api_key=secret")
        response = httpx.Response(404, request=request)
        error = httpx.HTTPStatusError("secret in URL", request=request, response=response)
        report = AsyncMock()
        with patch.object(phone.account_pool_authorization_service, "login_entry", new=AsyncMock(side_effect=error)):
            result = await phone._run_phone_verification(75, db, report=report)
        self.assertEqual(result["type"], "error")
        self.assertIn("404", result["error"])
        self.assertNotIn("secret", json.dumps(result))
        db.rollback.assert_awaited_once()

    async def test_save_conflict_never_reports_success(self):
        db = self.database()
        result = SimpleNamespace(phone_number="+233123456789", phone_rental_error="")
        with (
            patch.object(phone.account_pool_authorization_service, "login_entry", new=AsyncMock(return_value=result)),
            patch.object(phone.account_pool_authorization_service, "save_result", new=AsyncMock(return_value=False)),
        ):
            event = await phone._run_phone_verification(75, db, report=AsyncMock())
        self.assertEqual(event["type"], "error")
        self.assertIn("未保存结果", event["error"])
