import unittest

from app.services.openai_workspace import inspect_workspace_claims, resolve_workspace_id


class OpenAIWorkspaceTests(unittest.TestCase):
    def test_explicit_current_workspace_and_available_accounts_are_extracted(self):
        result = inspect_workspace_claims({
            "account_id": "external-team",
            "accounts": {
                "external-team": {"name": "External Team", "account_user_role": "member"},
                "personal": {"name": "Personal", "is_personal": True},
            },
        })

        self.assertEqual(result["status"], "workspace_ok")
        self.assertEqual(result["workspace_id"], "external-team")
        self.assertEqual(result["workspace_name"], "External Team")
        self.assertEqual(len(result["available_workspaces"]), 2)

    def test_default_workspace_is_used_when_current_id_is_absent(self):
        result = inspect_workspace_claims({
            "organizations": [
                {"id": "team-a", "title": "Team A"},
                {"id": "team-b", "title": "Team B", "is_default": True},
            ],
        })

        self.assertEqual(result["workspace_id"], "team-b")
        self.assertEqual(result["workspace_name"], "Team B")

    def test_multiple_workspaces_without_current_marker_are_ambiguous(self):
        result = inspect_workspace_claims({
            "organizations": [
                {"id": "team-a", "title": "Team A"},
                {"id": "team-b", "title": "Team B"},
            ],
        })

        self.assertEqual(result["status"], "workspace_ambiguous")
        self.assertEqual(result["workspace_id"], "")

    def test_account_without_workspace_is_explicit(self):
        result = inspect_workspace_claims({"email": "member@example.com"})

        self.assertEqual(result["status"], "no_workspace")
        self.assertEqual(result["available_workspaces"], [])

    def test_unique_organization_is_preferred_over_personal_workspace(self):
        scan = inspect_workspace_claims({
            "workspaces": [
                {"id": "team-a", "name": "Team A", "kind": "organization"},
                {"id": "personal-a", "name": None, "kind": "personal"},
            ],
        })

        self.assertEqual(resolve_workspace_id(scan), "team-a")


if __name__ == "__main__":
    unittest.main()
