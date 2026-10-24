"""OAuth login and persisted export state for account-pool entries."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any

from sqlalchemy import select, update

from app.models import AccountPoolEntry, AccountPoolWorkspace
from app.services.account_pool_authorization_models import (
    AccountPoolAuthorizationError, AccountPoolLoginResult,
)
from app.services.account_pool_credentials import AccountPoolCredentialError
from app.services.encryption import encryption_service
from app.services.member_authorization import CLIENT_ID, REDIRECT_URI
from app.services.member_authorization_payload import build_openai_export_payload
from app.services.openai_automatic_login import (
    AutomaticLoginRequest,
    OpenAIAutomaticLoginError,
)
from app.services.openai_workspace import token_claims, token_plan_type, token_workspace_id
from app.services.member_identity import credential_identity
from app.services.account_pool_profile import login_result_values
from app.services.operation_progress import ProgressCallback
from app.utils.time_utils import get_now
from app.services.account_pool_workspace_sync import (
    WorkspaceSyncError, persist_workspace_scan, synchronize_memberships,
)


class AccountPoolAuthorizationService:
    def __init__(self, credential_service, login_service, auth_client, *, team_service):
        self._credentials = credential_service
        self._login = login_service
        self._auth_client = auth_client
        self._teams = team_service

    async def login_entry(
        self,
        session,
        entry_id: int,
        workspace_id: str = "",
        *,
        phone_verification: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> AccountPoolLoginResult:
        entry = await session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            raise AccountPoolAuthorizationError("账号不存在")
        try:
            credentials = await self._credentials.get_credentials(session, entry.email)
        except AccountPoolCredentialError as exc:
            raise AccountPoolAuthorizationError(str(exc)) from exc
        self._validate_credentials(credentials)
        request = replace(
            self._request(entry, credentials, workspace_id, session),
            phone_verification=phone_verification,
            on_progress=on_progress,
        )
        try:
            result = await self._login.login(request)
        except OpenAIAutomaticLoginError as exc:
            raise AccountPoolAuthorizationError(str(exc)) from exc
        login_result = self._result(entry.email, result, request.account_id)
        return AccountPoolLoginResult(
            login_result.payload,
            login_result.workspace,
            login_result.verified_totp_factor_id,
            str(result.get("phone_number") or ""),
            str(result.get("phone_rental_error") or ""),
        )

    async def scan_entry(self, session, entry_id: int, *, identifier: str = "") -> dict[str, Any]:
        entry = await session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            raise AccountPoolAuthorizationError("账号不存在")
        credentials = await self._credentials.get_credentials(session, entry.email)
        self._validate_credentials(credentials)
        request = self._request(entry, credentials, "", session)
        if identifier:
            request = replace(request, identifier=identifier)
        try:
            return await self._login.verify_credentials(request)
        except OpenAIAutomaticLoginError as exc:
            raise AccountPoolAuthorizationError(str(exc)) from exc

    async def refresh_team_list(
        self, session, entry_id: int, *, scan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = await session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            raise AccountPoolAuthorizationError("账号不存在")
        version = (entry.password_encrypted, entry.two_factor_secret_encrypted)
        scan = scan or await self.scan_entry(session, entry_id)
        discovered = self._team_options(scan.get("available_workspaces") or [])

        async def authorize_identity(account_id):
            result = await self.login_entry(session, entry_id, account_id)
            if not credential_identity(result.payload, entry.email, account_id):
                raise AccountPoolAuthorizationError("成员授权未返回所选 Team 的用户 ID，无法核对成员身份")
            saved = await self.save_result(session, entry_id, result,
                                           credential_version=version, update_current=False)
            if not saved:
                raise AccountPoolAuthorizationError("账号凭据已变化，请重新刷新 Team")

        try:
            options = await synchronize_memberships(
                session, entry, discovered=discovered, team_service=self._teams,
                authorize_identity=authorize_identity,
            )
            return await persist_workspace_scan(
                session, entry, discovered=options, credential_version=version,
            )
        except WorkspaceSyncError as exc:
            raise AccountPoolAuthorizationError(str(exc)) from exc

    @staticmethod
    def _team_options(discovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
        options = []
        for item in discovered:
            if not isinstance(item, dict):
                raise AccountPoolAuthorizationError("Team 列表格式错误")
            workspace_id = str(item.get("id") or "").strip()
            if not workspace_id:
                continue
            name = str(item.get("name") or workspace_id).strip()
            if len(workspace_id) > 100 or len(name) > 255:
                raise AccountPoolAuthorizationError("Team ID 或名称超出存储长度")
            options.append({"id": workspace_id, "name": name,
                            "is_personal": bool(item.get("is_personal"))})
        return options

    async def save_result(
        self,
        session,
        entry_id: int,
        result: AccountPoolLoginResult,
        *,
        credential_version: tuple[str | None, str | None] | None = None,
        liveness: tuple[str, str] | None = None,
        update_current: bool = True,
        workspace_state: dict[str, Any] | None = None,
    ) -> bool:
        now = get_now()
        entry = await session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            return False
        values = login_result_values(
            entry, result, cipher=encryption_service, now=now,
            update_current=update_current, workspace_state=workspace_state,
        )
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
        if saved.rowcount:
            if result.phone_number:
                entry.phone_number_encrypted = encryption_service.encrypt_token(result.phone_number)
                entry.phone_verified_at = now
            await self._save_workspace(session, entry_id, result, now)
        await session.commit()
        return bool(saved.rowcount)

    @staticmethod
    async def _save_workspace(session, entry_id, result, now) -> None:
        workspace_id = str(result.workspace.get("workspace_id") or "").strip()
        if not workspace_id:
            return
        row = (await session.execute(select(AccountPoolWorkspace).where(
            AccountPoolWorkspace.account_pool_id == entry_id,
            AccountPoolWorkspace.workspace_id == workspace_id,
        ))).scalar_one_or_none()
        if row is None:
            row = AccountPoolWorkspace(account_pool_id=entry_id, workspace_id=workspace_id)
            session.add(row)
        row.name = result.workspace.get("workspace_name") or None
        row.is_personal = bool(result.workspace.get("is_personal"))
        row.status = result.workspace.get("status") or "workspace_unknown"
        row.export_json_encrypted = encryption_service.encrypt_token(
            json.dumps(result.payload, ensure_ascii=False)
        )
        row.checked_at = now
        row.json_updated_at = now

    @staticmethod
    def _validate_credentials(credentials: dict[str, str] | None) -> None:
        if not credentials or not credentials.get("password"):
            raise AccountPoolAuthorizationError("未保存登录密码")
        if not credentials.get("two_factor_secret"):
            raise AccountPoolAuthorizationError("未保存 2FA 密钥")

    def _request(self, entry, credentials, workspace_id, session) -> AutomaticLoginRequest:
        draft = self._auth_client.create_oauth_authorize_url(CLIENT_ID, REDIRECT_URI)
        draft.update({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI})
        target_id = str(workspace_id or "").strip()
        return AutomaticLoginRequest(
            email=entry.email,
            password=credentials["password"],
            totp_secret=credentials["two_factor_secret"],
            account_id=target_id,
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
        token_id = token_workspace_id(access_token)
        if requested_id and token_id and requested_id != token_id:
            raise AccountPoolAuthorizationError("登录返回的空间与所选 Team 不一致")
        actual_id = str(requested_id or workspace.get("workspace_id") or "").strip()
        if workspace.get("status") != "no_workspace":
            actual_id = actual_id or token_id
        plan_type = token_plan_type(claims, identity)
        is_personal = self._is_personal_workspace(workspace, plan_type)
        status = "personal_account" if is_personal else (
            "workspace_ok" if actual_id else "no_workspace"
        )
        workspace.update({
            "status": status,
            "workspace_id": actual_id,
            "workspace_name": (
                "个人账户" if is_personal
                else str(workspace.get("workspace_name") or "")
            ),
            "is_personal": is_personal,
            "plan_type": plan_type or ("free" if is_personal else "team"),
        })
        exported = {
            key: str(result.get(key) or "")
            for key in ("access_token", "refresh_token", "id_token")
        }
        exported["client_id"] = CLIENT_ID
        payload = build_openai_export_payload(
            email=email,
            account_id=actual_id,
            credentials=exported,
            claims=claims,
            identity=identity,
            plan_type=workspace["plan_type"],
        )
        return AccountPoolLoginResult(
            payload=payload,
            workspace=workspace,
            verified_totp_factor_id=str(result.get("verified_totp_factor_id") or ""),
            phone_number=str(result.get("phone_number") or ""),
            phone_rental_error=str(result.get("phone_rental_error") or ""),
        )

    @staticmethod
    def _is_personal_workspace(workspace: dict[str, Any], plan_type: str) -> bool:
        if workspace.get("is_personal") or workspace.get("status") == "personal_account":
            return True
        return plan_type in {"free", "plus", "pro", "personal"}

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
