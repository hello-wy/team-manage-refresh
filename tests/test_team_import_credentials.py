import unittest
from unittest.mock import AsyncMock, patch

from app.routes import admin


class TeamImportCredentialsTests(unittest.IsolatedAsyncioTestCase):
    async def test_credentials_login_imports_team_with_oauth_tokens(self):
        login_result = {
            "access_token": "valid-access-token",
            "id_token": "valid-id-token",
            "refresh_token": "refresh-token",
            "session_token": "session-token",
        }
        imported = {"success": True, "team_id": 23, "message": "成功导入 1 个 Team"}
        with (
            patch.object(admin.chatgpt_service, "create_oauth_authorize_url", return_value={
                "authorize_url": "https://auth.openai.com/oauth/authorize",
                "state": "state",
                "code_verifier": "verifier",
            }) as create_oauth,
            patch.object(admin.automatic_login_service, "login", new=AsyncMock(return_value=login_result)) as login,
            patch.object(admin.team_service.jwt_parser, "extract_email", return_value="owner@example.com"),
            patch.object(admin.team_service, "import_team_single", new=AsyncMock(return_value=imported)) as import_team,
        ):
            response = await admin.team_import(
                admin.TeamImportRequest(
                    import_type="credentials", email=" Owner@Example.com ",
                    password=" owner-password ", two_factor_secret="JBSWY3DPEHPK3PXP",
                    account_id="team-account", pool_type="welfare",
                ),
                db=object(), current_user={"username": "admin"},
            )

        self.assertEqual(response.status_code, 200)
        create_oauth.assert_called_once_with(admin.MEMBER_OAUTH_CLIENT_ID, admin.MEMBER_OAUTH_REDIRECT_URI)
        request = login.await_args.args[0]
        self.assertEqual(request.email, "owner@example.com")
        self.assertEqual(request.password, " owner-password ")
        self.assertEqual(request.totp_secret, "JBSWY3DPEHPK3PXP")
        self.assertEqual(request.account_id, "team-account")
        self.assertNotIn("password", request.identifier)
        import_team.assert_awaited_once_with(
            access_token="valid-access-token", db_session=unittest.mock.ANY,
            email="owner@example.com", id_token="valid-id-token",
            refresh_token="refresh-token", session_token="session-token",
            client_id=admin.MEMBER_OAUTH_CLIENT_ID, account_id="team-account", pool_type="welfare",
        )

    async def test_rejects_bad_2fa_secret_before_network_login(self):
        with patch.object(admin.automatic_login_service, "login", new=AsyncMock()) as login:
            response = await admin.team_import(
                admin.TeamImportRequest(
                    import_type="credentials", email="owner@example.com",
                    password="password", two_factor_secret="INVALID*SECRET",
                ),
                db=object(), current_user={"username": "admin"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(login.await_count)

    async def test_rejects_login_token_for_different_email(self):
        with (
            patch.object(admin.chatgpt_service, "create_oauth_authorize_url", return_value={
                "authorize_url": "https://auth.openai.com/oauth/authorize",
                "state": "state", "code_verifier": "verifier",
            }),
            patch.object(admin.automatic_login_service, "login", new=AsyncMock(return_value={
                "access_token": "valid-access-token", "id_token": "valid-id-token",
            })),
            patch.object(admin.team_service.jwt_parser, "extract_email", return_value="other@example.com"),
            patch.object(admin.team_service, "import_team_single", new=AsyncMock()) as import_team,
        ):
            response = await admin.team_import(
                admin.TeamImportRequest(
                    import_type="credentials", email="owner@example.com",
                    password="password", two_factor_secret="JBSWY3DPEHPK3PXP",
                ),
                db=object(), current_user={"username": "admin"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("邮箱与填写邮箱不一致", response.body.decode())
        self.assertFalse(import_team.await_count)


if __name__ == "__main__":
    unittest.main()
