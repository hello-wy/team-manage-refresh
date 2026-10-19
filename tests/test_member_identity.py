"""Member identities must match the saved email and the live Team workspace."""
import base64
import json
import unittest

from app.services.member_identity import credential_identity


def token_credentials(*, email="member@example.com", account_id="team", user_id="user-1"):
    claims = {"https://api.openai.com/profile": {"email": email},
              "https://api.openai.com/auth": {
                  "chatgpt_account_id": account_id, "chatgpt_user_id": user_id,
              }}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"access_token": f"header.{encoded}.signature"}


class MemberIdentityTests(unittest.TestCase):
    def test_export_and_raw_credentials_resolve_the_same_identity(self):
        credentials = token_credentials()
        for payload in (credentials, {"accounts": [{"credentials": credentials}]}):
            with self.subTest(exported="accounts" in payload):
                self.assertEqual(credential_identity(payload, "member@example.com", "team"),
                                 ("user-1", "member@example.com"))

    def test_other_team_credentials_do_not_establish_membership(self):
        self.assertIsNone(credential_identity(token_credentials(account_id="other"),
                                             "member@example.com", "team"))

    def test_mismatched_email_is_explicitly_rejected(self):
        with self.assertRaisesRegex(ValueError, "邮箱与账号不一致"):
            credential_identity(token_credentials(email="other@example.com"),
                                "member@example.com", "team")

    def test_missing_or_unreadable_token_does_not_invent_an_identity(self):
        for credentials in ({"access_token": "unreadable"}, token_credentials(user_id="")):
            with self.subTest(credentials=credentials):
                self.assertIsNone(credential_identity(credentials, "member@example.com", "team"))
