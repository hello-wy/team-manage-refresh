import unittest

import httpx

from app.services.official_key_balance import (
    BILLING_CREDIT_GRANTS_PATH,
    OPENAI_BASE_URL,
    OfficialKeyBalanceError,
    OfficialKeyBalanceService,
    normalize_balance,
)


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url):
        self.request = url
        if self.error:
            raise self.error
        return self.response


class OfficialKeyBalanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_official_endpoint_with_bearer_key(self):
        client = FakeClient(httpx.Response(200, json={
            "total_granted": 20, "total_used": "3.25", "total_available": 16.75,
        }))
        client_options = {}

        def factory(**kwargs):
            client_options.update(kwargs)
            return client

        service = OfficialKeyBalanceService(factory)

        result = await service.query("secret-key")

        self.assertEqual(client.request, f"{OPENAI_BASE_URL}{BILLING_CREDIT_GRANTS_PATH}")
        self.assertEqual(client_options["headers"], {"Authorization": "Bearer secret-key"})
        self.assertEqual(result, {
            "total_granted": "20", "total_used": "3.25", "total_available": "16.75",
            "currency": "USD",
        })

    async def test_network_and_http_failures_are_explicit(self):
        client = FakeClient(error=httpx.ConnectError("offline"))
        with self.assertRaisesRegex(OfficialKeyBalanceError, "无法连接"):
            await OfficialKeyBalanceService(lambda **kwargs: client).query("secret-key")

        response = httpx.Response(401, json={"error": "invalid"})
        with self.assertRaisesRegex(OfficialKeyBalanceError, "HTTP 401"):
            await OfficialKeyBalanceService(lambda **kwargs: FakeClient(response)).query("secret-key")

    async def test_query_many_keeps_each_key_result_visible(self):
        class Service(OfficialKeyBalanceService):
            async def query(self, api_key):
                if api_key == "bad":
                    raise OfficialKeyBalanceError("余额查询失败")
                return {"total_available": "5", "total_granted": "5",
                        "total_used": "0", "currency": "USD"}

        result = await Service().query_many({1: "good", 2: "bad"})

        self.assertEqual(result[1]["status"], "ok")
        self.assertEqual(result[1]["balance"]["total_available"], "5")
        self.assertEqual(result[2], {"status": "error", "error": "余额查询失败"})

    async def test_upstream_authentication_reason_is_visible_and_key_is_redacted(self):
        api_key = "sk-proj-test-private-key"
        response = httpx.Response(401, json={"error": {
            "message": f"This endpoint requires a session key; received {api_key}",
        }})
        service = OfficialKeyBalanceService(lambda **kwargs: FakeClient(response))

        with self.assertRaises(OfficialKeyBalanceError) as caught:
            await service.query(api_key)

        self.assertIn("HTTP 401", str(caught.exception))
        self.assertIn("requires a session key", str(caught.exception))
        self.assertNotIn(api_key, str(caught.exception))

    async def test_decimal_precision_and_negative_balance_are_preserved(self):
        response = httpx.Response(200, content=(
            '{"total_granted": 20, "total_used": 20.000000000000000001,'
            ' "total_available": -0.000000000000000001}'
        ))
        result = await OfficialKeyBalanceService(
            lambda **kwargs: FakeClient(response)
        ).query("test-key")

        self.assertEqual(result["total_available"], "-0.000000000000000001")

    async def test_error_in_success_response_is_exposed(self):
        response = httpx.Response(200, json={"error": {"message": "billing access denied"}})
        service = OfficialKeyBalanceService(lambda **kwargs: FakeClient(response))
        with self.assertRaisesRegex(OfficialKeyBalanceError, "billing access denied"):
            await service.query("test-key")

    def test_nonfinite_and_boolean_amounts_are_rejected(self):
        for invalid in (True, "NaN", "Infinity", [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(OfficialKeyBalanceError):
                normalize_balance({
                    "total_granted": 20, "total_used": 3, "total_available": invalid,
                })

    def test_invalid_shape_is_explicit(self):
        with self.assertRaisesRegex(OfficialKeyBalanceError, "缺少 total_used"):
            normalize_balance({"total_granted": 1, "total_available": 1})


if __name__ == "__main__":
    unittest.main()
