"""OAuth login and persisted export state for account-pool entries."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import update

from app.models import AccountPoolEntry
from app.services.account_pool_credentials import AccountPoolCredentialError
from app.services.encryption import encryption_service
from app.services.member_authorization import CLIENT_ID, REDIRECT_URI
from app.services.member_authorization_payload import build_openai_export_payload
from app.services.openai_automatic_login import (
    AutomaticLoginRequest,
    OpenAIAutomaticLoginError,
)
from app.services.openai_workspace import token_claims, token_workspace_id
from app.utils.time_utils import get_now


class AccountPoolAuthorizationError(ValueError):
    pass


@dataclass(frozen=True)
class AccountPoolLoginResult:
    payload: dict[str, Any]
    workspace: dict[str, Any]


class AccountPoolAuthorizationService:
    def __init__(self, credential_service, login_service, auth_client):
        self._credentials = credential_service
        self._login = login_service
        self._auth_client = auth_client

    async def login_entry(
        self,
        session,
        entry_id: int,
        workspace_id: str = "",
    ) -> AccountPoolLoginResult:
        entry = await session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            raise AccountPoolAuthorizationError("账号不存在")
        try:
            credentials = await self._credentials.get_credentials(session, entry.email)
        except AccountPoolCredentialError as exc:
            raise AccountPoolAuthorizationError(str(exc)) from exc
        self._validate_credentials(credentials)
        request = self._request(entry, credentials, workspace_id, session)
        try:
            result = await self._login.login(request)
        except OpenAIAutomaticLoginError as exc:
            raise AccountPoolAuthorizationError(str(exc)) from exc
        return self._result(entry.email, result, request.account_id)

    async def save_result(
        self,
        session,
        entry_id: int,
        result: AccountPoolLoginResult,
        *,
        credential_version: tuple[str | None, str | None] | None = None,
        liveness: tuple[str, str] | None = None,
    ) -> bool:
        now = get_now()
        values = {
            "workspace_id": result.workspace.get("workspace_id") or None,
            "workspace_name": result.workspace.get("workspace_name") or None,
            "workspace_status": result.workspace.get("status") or "workspace_unknown",
            "workspace_checked_at": now,
            "workspace_state_json": json.dumps(result.workspace, ensure_ascii=False),
            "export_json_encrypted": encryption_service.encrypt_token(
                json.dumps(result.payload, ensure_ascii=False)
            ),
            "export_json_updated_at": now,
        }
        if liveness:
            values.update({
                "liveness_status": liveness[0],
                "liveness_message": liveness[1],
                "liveness_checked_at": now,
            })
        statement = update(AccountPoolEntry).where(
            AccountPoolEntry.id == entry_id,
            AccountPoolEntry.deleted_at.is_(None),
        )
        if credential_version:
            statement = statement.where(
                AccountPoolEntry.password_encrypted == credential_version[0],
                AccountPoolEntry.two_factor_secret_encrypted == credential_version[1],
            )
        saved = await session.execute(statement.values(**values))
        await session.commit()
        return bool(saved.rowcount)

    @staticmethod
    def _validate_credentials(credentials: dict[str, str] | None) -> None:
        if not credentials or not credentials.get("password"):
            raise AccountPoolAuthorizationError("未保存登录密码")
        if not credentials.get("two_factor_secret"):
            raise AccountPoolAuthorizationError("未保存 2FA 密钥")

    def _request(self, entry, credentials, workspace_id, session) -> AutomaticLoginRequest:
        draft = self._auth_client.create_oauth_authorize_url(CLIENT_ID, REDIRECT_URI)
        draft.update({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI})
        return AutomaticLoginRequest(
            email=entry.email,
            password=credentials["password"],
            totp_secret=credentials["two_factor_secret"],
            account_id=str(workspace_id or entry.workspace_id or "").strip(),
            oauth_draft=draft,
            db_session=session,
            identifier=f"account-pool-login-{entry.id}",
        )

    def _result(self, email: str, result: dict[str, Any], requested_id: str) -> AccountPoolLoginResult:
        access_token = str(result.get("access_token") or "")
        id_token = str(result.get("id_token") or "")
        claims = token_claims(access_token)
        identity = token_claims(id_token)
        self._validate_tokens(result, claims)
        token_email = self._token_email(claims, identity)
        if token_email and token_email != email:
            raise AccountPoolAuthorizationError("自动登录返回了其他账号的凭据")
        workspace = dict(result.get("workspace") or {})
        actual_id = str(requested_id or workspace.get("workspace_id") or "").strip()
        if workspace.get("status") != "no_workspace":
            actual_id = actual_id or token_workspace_id(access_token)
        workspace.update({
            "status": "workspace_ok" if actual_id else "no_workspace",
            "workspace_id": actual_id,
            "workspace_name": str(workspace.get("workspace_name") or ""),
        })
        exported = {
            key: str(result.get(key) or "")
            for key in ("access_token", "refresh_token", "id_token")
        }
        exported["client_id"] = CLIENT_ID
        payload = build_openai_export_payload(
            email=email,
            account_id=actual_id,
            account_name=workspace.get("workspace_name") or actual_id or "Personal",
            credentials=exported,
            claims=claims,
            identity=identity,
            plan_type="team" if actual_id else "free",
        )
        return AccountPoolLoginResult(payload=payload, workspace=workspace)

    @staticmethod
    def _validate_tokens(result: dict[str, Any], claims: dict[str, Any]) -> None:
        if not result.get("access_token") or not claims:
            raise AccountPoolAuthorizationError("自动登录未返回有效的 Access Token")
        if not result.get("refresh_token"):
            raise AccountPoolAuthorizationError("自动登录未返回 Refresh Token")
        expires_at = claims.get("exp")
        if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
            raise AccountPoolAuthorizationError("自动登录返回的 Access Token 已过期")

    @staticmethod
    def _token_email(claims: dict[str, Any], identity: dict[str, Any]) -> str:
        for data in (claims, identity):
            profile = data.get("https://api.openai.com/profile")
            if not isinstance(profile, dict):
                profile = {}
            value = str(data.get("email") or profile.get("email") or "").strip().lower()
            if value:
                return value
        return ""
