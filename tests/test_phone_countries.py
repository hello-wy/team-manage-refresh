import json
import unittest
from unittest.mock import patch

import httpx

from app.routes import account_pool_phone as phone


class PhoneCountriesTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_list_preserves_zero_id_and_uses_chinese_names(self):
        def upstream(request):
            self.assertEqual(dict(request.url.params), {"action": "getCountries"})
            return httpx.Response(200, json={
                "63": {"id": 63, "chn": "捷克共和国", "eng": "Czech", "visible": 1},
                "0": {"id": 0, "chn": "俄罗斯", "eng": "Russia", "visible": 1},
                "1": {"id": 1, "chn": "隐藏国家", "eng": "Hidden", "visible": 0},
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        with patch.object(phone.httpx, "AsyncClient", return_value=client):
            response = await phone.phone_verification_countries(current_user={})
        self.assertEqual(json.loads(response.body), {"success": True, "countries": [
            {"id": "0", "name": "俄罗斯"}, {"id": "63", "name": "捷克共和国"},
        ]})

    async def test_upstream_failure_is_explicit(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(503)
        ))
        with patch.object(phone.httpx, "AsyncClient", return_value=client):
            response = await phone.phone_verification_countries(current_user={})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(json.loads(response.body)["success"])

    def test_rejects_malformed_country_list(self):
        for payload in ([], {}, {"63": {"id": "not-a-number"}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                phone._country_options(payload)
