"""Refresh a Team and recover invalid owner credentials with one account-pool login."""
from dataclasses import dataclass
from typing import Any

from app.models import Team
from app.services.account_pool_credentials import AccountPoolCredentialError
from app.services.member_authorization import CLIENT_ID, REDIRECT_URI
from app.services.openai_automatic_login import AutomaticLoginRequest, OpenAIAutomaticLoginError
from app.services.openai_workspace import token_workspace_id
from app.utils.seat_lock import seat_account_lock

ABNORMAL_STATUSES = frozenset({"error", "banned"})
INVALID_LOGIN_STATUSES = ABNORMAL_STATUSES | {"expired"}
INVALID_LOGIN_ERRORS = frozenset({
    "token_refresh_failed", "token_identity_mismatch", "team_banned",
    "account_info_unavailable_after_refresh", "team_marked_error",
})


class TeamRefreshLoginError(ValueError):
    pass


@dataclass(frozen=True)
class TeamRefreshDependencies:
    teams: Any
    credentials: Any
    login: Any
    cipher: Any
    jwt: Any


class TeamRefreshService:
    def __init__(self, dependencies: TeamRefreshDependencies):
        self._deps = dependencies

    async def refresh(self, team_id, db, *, force_refresh=False):
        if force_refresh:
            return await self._deps.teams.sync_team_info(team_id, db, force_refresh=True)
        team = await db.get(Team, team_id)
        if team is None:
            return await self._deps.teams.sync_team_info(team_id, db)
        async with seat_account_lock(f"team-refresh-login:{team.email.strip().lower()}"):
            await db.refresh(team)
            if team.status in ABNORMAL_STATUSES:
                return await self._recover(team, db)
            result = await self._deps.teams.sync_team_info(team_id, db)
            await db.refresh(team)
            if result.get("success"):
                return result
            invalid_login = (team.status in INVALID_LOGIN_STATUSES
                             or result.get("error_code") in INVALID_LOGIN_ERRORS)
            if invalid_login:
                return await self._recover(team, db)
            return result

    async def _recover(self, team, db):
        try:
            request = await self._request(team, db)
            try:
                result = await self._deps.login.login(request)
            finally:
                await self._deps.teams.chatgpt_service.clear_session(request.identifier)
            self._validate_identity(team, result)
            await self._deps.teams.chatgpt_service.clear_session(team.email)
            await self._verify_workspace(team, result, db=db)
            await self._save_credentials(team, result, db=db)
        except (AccountPoolCredentialError, OpenAIAutomaticLoginError, TeamRefreshLoginError) as exc:
            return {"success": False, "error_code": "team_relogin_failed",
                    "error": f"异常账号重新登录失败：{exc}", "relogin_attempted": True}
        result = await self._deps.teams.sync_team_info(team.id, db)
        if not result.get("success"):
            return {**result, "relogin_attempted": True,
                    "error": f"重新登录凭据已回填，但 Team 状态复核失败：{result.get('error')}"}
        return {**result, "relogin_attempted": True,
                "message": f"已重新登录并更新登录状态；{result['message']}"}

    async def _request(self, team, db):
        if not team.account_id:
            raise TeamRefreshLoginError("Team 未配置工作区 ID")
        email = team.email.strip().lower()
        account = await self._deps.credentials.get_credentials(db, email)
        if not account or not account.get("password"):
            raise TeamRefreshLoginError("账号号池未保存该账号的登录密码")
        if not account.get("two_factor_secret"):
            raise TeamRefreshLoginError("账号号池未保存该账号的 2FA 密钥")
        draft = self._deps.teams.chatgpt_service.create_oauth_authorize_url(CLIENT_ID, REDIRECT_URI)
        return AutomaticLoginRequest(
            email=email, password=account["password"],
            totp_secret=account["two_factor_secret"], account_id=team.account_id,
            oauth_draft={**draft, "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI},
            db_session=db, identifier=f"team-refresh-login-{team.id}",
        )

    def _validate_identity(self, team, result):
        access = result.get("access_token") or ""
        jwt = self._deps.jwt
        if not result.get("success") or not jwt.decode_token(access) or jwt.is_token_expired(access):
            raise TeamRefreshLoginError("登录未返回有效的 Access Token")
        if not result.get("refresh_token"):
            raise TeamRefreshLoginError("登录未返回 Refresh Token")
        email = team.email.strip().lower()
        self._validate_email(access, email)
        identity = result.get("id_token")
        if identity:
            self._validate_email(identity, email)
        self._validate_workspace(team.account_id, result)

    def _validate_email(self, token, email):
        if str(self._deps.jwt.extract_email(token) or "").strip().lower() != email:
            raise TeamRefreshLoginError("登录返回的 Token 邮箱与当前 Team 不一致")

    @staticmethod
    def _validate_workspace(account_id, result):
        access = result["access_token"]
        workspace_id = str((result.get("workspace") or {}).get("workspace_id") or "")
        if token_workspace_id(access) != account_id:
            raise TeamRefreshLoginError("登录返回的工作区与当前 Team 不一致")
        if workspace_id and workspace_id != account_id:
            raise TeamRefreshLoginError("登录选择的工作区与当前 Team 不一致")

    async def _verify_workspace(self, team, result, *, db):
        account = await self._deps.teams.chatgpt_service.get_account_info(
            result["access_token"], db, identifier=team.email,
        )
        if not account.get("success"):
            raise TeamRefreshLoginError(
                f"登录后账号状态检测失败：{account.get('error') or account.get('error_code')}"
            )
        if not any(item.get("account_id") == team.account_id for item in account["accounts"]):
            raise TeamRefreshLoginError("登录账号已不在当前 Team，无法回填该工作区登录状态")

    async def _save_credentials(self, team, result, *, db):
        cipher = self._deps.cipher
        team.access_token_encrypted = cipher.encrypt_token(result["access_token"])
        team.refresh_token_encrypted = cipher.encrypt_token(result["refresh_token"])
        team.id_token_encrypted = (cipher.encrypt_token(result["id_token"])
                                   if result.get("id_token") else None)
        team.session_token_encrypted = None
        team.client_id = CLIENT_ID
        await db.commit()
