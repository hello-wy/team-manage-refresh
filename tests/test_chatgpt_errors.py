"""Retain structured upstream errors, including detail-wrapped billing errors."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.services.chatgpt import ChatGPTService

class ChatGPTErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_billing_error_code_is_preserved_for_all_supported_response_shapes(self):
        error = {'code': 'workspace_subscription_delinquent', 'message': 'Team has a delinquent subscription'}
        for body in ({'detail': error}, {'error': error}, error):
            with self.subTest(body=body):
                response = SimpleNamespace(status_code=401, text=json.dumps(body), json=lambda:body)
                session = SimpleNamespace(post=AsyncMock(return_value=response))
                service = ChatGPTService()
                service._get_session = AsyncMock(return_value=session)
                service.clear_session = AsyncMock()
                result = await service._make_request('POST', 'https://chatgpt.com/test', {})
                self.assertFalse(result['success'])
                self.assertEqual(result['error_code'], 'workspace_subscription_delinquent')
                session.post.assert_awaited_once()
                service.clear_session.assert_not_awaited()
