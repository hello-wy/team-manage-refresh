import unittest
from unittest.mock import AsyncMock

from app.services.chatgpt import ChatGPTService


class ChatGPTAccountInfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_business_plan_is_returned_as_team_workspace(self):
        service = ChatGPTService()
        service._make_request = AsyncMock(return_value={
            "success": True,
            "data": {
                "accounts": {
                    "business-account": {
                        "account": {
                            "name": "Business Workspace",
                            "plan_type": "business",
                            "account_user_role": "standard-user",
                        },
                        "entitlement": {
                            "subscription_plan": "business",
                            "has_active_subscription": True,
                        },
                    },
                    "personal-account": {
                        "account": {"name": "Personal", "plan_type": "free"},
                        "entitlement": {},
                    },
                }
            },
        })

        result = await service.get_account_info("access-token", object())

        self.assertTrue(result["success"])
        self.assertEqual(len(result["accounts"]), 1)
        self.assertEqual(result["accounts"][0]["account_id"], "business-account")
        self.assertEqual(result["accounts"][0]["plan_type"], "business")

    async def test_self_serve_business_prolite_is_returned_as_team_workspace(self):
        service = ChatGPTService()
        service._make_request = AsyncMock(return_value={
            "success": True,
            "data": {
                "accounts": {
                    "team-account": {
                        "account": {
                            "name": "myWorkspace",
                            "plan_type": "self_serve_business_prolite",
                            "account_user_role": "account-owner",
                        },
                        "entitlement": {
                            "has_active_subscription": True,
                        },
                    },
                    "personal-account": {
                        "account": {"plan_type": "free"},
                        "entitlement": {"has_active_subscription": False},
                    },
                }
            },
        })

        result = await service.get_account_info("access-token", object())

        self.assertTrue(result["success"])
        self.assertEqual(len(result["accounts"]), 1)
        self.assertEqual(result["accounts"][0]["account_id"], "team-account")
        self.assertEqual(result["accounts"][0]["plan_type"], "self_serve_business_prolite")


if __name__ == "__main__":
    unittest.main()
