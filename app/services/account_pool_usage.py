"""Fetch live ChatGPT usage for accounts saved in the account pool."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from curl_cffi.requests import AsyncSession as CurlAsyncSession, RequestsError
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolWorkspace, MemberAuthorization
from app.services.encryption import encryption_service
from app.services.settings import settings_service
from app.services.wham_usage import parse_usage
from app.utils.proxy import build_curl_cffi_proxies

logger = logging.getLogger(__name__)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USAGE_HEADERS = {"Accept": "application/json", "Referer": "https://chatgpt.com/"}


class AccountPoolUsageService:
    def __init__(self, client_factory=CurlAsyncSession, timeout: float = 30.0, concurrency: int = 8):
        self.client_factory = client_factory
        self.timeout = timeout
        self.concurrency = concurrency

    @staticmethod
    def _entry_token(entry: AccountPoolEntry | None, team_space_id: str = "",
                     workspace: AccountPoolWorkspace | None = None) -> tuple[str, str] | None:
        if not entry:
            return None
        encrypted = workspace.export_json_encrypted if workspace else entry.export_json_encrypted
        if team_space_id and not workspace and entry.workspace_id != team_space_id:
            return None
        if not encrypted:
            return None
        try:
            payload = json.loads(encryption_service.decrypt_token(encrypted))
            accounts = payload.get("accounts") or []
            selected = None
            for account in accounts:
                credentials = account.get("credentials") if isinstance(account, dict) else None
                account_id = str((credentials or {}).get("chatgpt_account_id") or "").strip()
                if team_space_id and account_id == team_space_id:
                    selected = credentials
                    break
                if not team_space_id and selected is None and isinstance(credentials, dict):
                    selected = credentials
            token = str((selected or {}).get("access_token") or "").strip()
            account_id = str((selected or {}).get("chatgpt_account_id") or "").strip()
            return (token, account_id) if token else None
        except Exception:
            return None

    @staticmethod
    def _authorization_token(record: MemberAuthorization | None, team_space_id: str):
        if not record or not record.credentials_encrypted:
            return None
        try:
            payload = json.loads(encryption_service.decrypt_token(record.credentials_encrypted))
        except (ValueError, TypeError, InvalidToken) as exc:
            logger.warning("成员授权凭据无法解析: authorization_id=%s error=%s", record.id, exc)
            return None
        if not isinstance(payload, dict):
            logger.warning("成员授权凭据格式错误: authorization_id=%s", record.id)
            return None
        credentials = payload.get("credentials", payload)
        if not isinstance(credentials, dict):
            logger.warning("成员授权字段格式错误: authorization_id=%s", record.id)
            return None
        account_id = str(credentials.get("chatgpt_account_id") or "").strip()
        token = str(credentials.get("access_token") or "").strip()
        if account_id != team_space_id or not token:
            return None
        return token, account_id

    @staticmethod
    async def _proxies(db: AsyncSession) -> dict[str, str] | None:
        config = await settings_service.get_proxy_config(db)
        return build_curl_cffi_proxies(config["proxy"]) if config["enabled"] else None

    async def _check_token(self, token_data: tuple[str, str] | None,
                           proxies: dict[str, str] | None = None) -> dict[str, Any]:
        if not token_data:
            return {"status": "unavailable", "error": "账号池没有可用 JSON"}
        token, account_id = token_data
        headers = {**USAGE_HEADERS, "Authorization": f"Bearer {token}"}
        if account_id:
            headers["Chatgpt-Account-Id"] = account_id
        try:
            async with self.client_factory(
                timeout=self.timeout, impersonate="chrome110", proxies=proxies,
            ) as client:
                response = await client.get(USAGE_URL, headers=headers)
            if response.status_code == 401:
                return {"status": "invalid", "error": "Token 无效"}
            if response.status_code < 200 or response.status_code >= 300:
                return {"status": "unknown", "error": f"HTTP {response.status_code}"}
            try:
                body = response.json()
            except ValueError:
                return {"status": "error", "error": "额度响应格式错误"}
            usage = parse_usage(body)
            if usage is None:
                return {"status": "unknown", "error": "未找到额度窗口"}
            return {"status": "ok", "error": None, **usage}
        except (RequestsError, TimeoutError) as exc:
            return {"status": "error", "error": type(exc).__name__}

    async def check_email(self, db: AsyncSession, email: str, team_space_id: str = "") -> dict[str, Any]:
        normalized_email = str(email or "").strip().lower()
        result = await db.execute(
            select(AccountPoolEntry).where(
                AccountPoolEntry.email == normalized_email,
                AccountPoolEntry.deleted_at.is_(None),
            )
        )
        entry = result.scalar_one_or_none()
        workspace = None
        if entry and team_space_id:
            workspace = (await db.execute(select(AccountPoolWorkspace).where(
                AccountPoolWorkspace.account_pool_id == entry.id,
                AccountPoolWorkspace.workspace_id == team_space_id,
            ))).scalar_one_or_none()
        token = self._entry_token(entry, team_space_id, workspace)
        if not token and team_space_id:
            record = (await db.execute(select(MemberAuthorization).where(
                MemberAuthorization.email == normalized_email,
                MemberAuthorization.account_id == team_space_id,
            ))).scalar_one_or_none()
            token = self._authorization_token(record, team_space_id)
        return await self._check_token(token, await self._proxies(db))

    @staticmethod
    def _normalize_accounts(accounts: list[str | tuple[str, str]]) -> list[tuple[Any, str, str]]:
        normalized: list[tuple[Any, str, str]] = []
        for item in accounts:
            if isinstance(item, tuple):
                email = str(item[0] or "").strip().lower()
                team_space_id = str(item[1] or "").strip()
                key: Any = (email, team_space_id)
            else:
                email = str(item or "").strip().lower()
                team_space_id = ""
                key = email
            if email and key not in [value[0] for value in normalized]:
                normalized.append((key, email, team_space_id))
        return normalized

    async def _tokens_for_accounts(
        self, db: AsyncSession, normalized: list[tuple[Any, str, str]],
    ) -> list[tuple[Any, tuple[str, str] | None]]:
        emails = list(dict.fromkeys(value[1] for value in normalized))
        rows = await db.execute(
            select(AccountPoolEntry).where(
                AccountPoolEntry.email.in_(emails),
                AccountPoolEntry.deleted_at.is_(None),
            )
        )
        entries = {entry.email: entry for entry in rows.scalars().all()}
        requested_ids = {space_id for _, _, space_id in normalized if space_id}
        workspaces = {}
        if entries and requested_ids:
            saved = await db.execute(select(AccountPoolWorkspace).where(
                AccountPoolWorkspace.account_pool_id.in_([entry.id for entry in entries.values()]),
                AccountPoolWorkspace.workspace_id.in_(requested_ids),
            ))
            workspaces = {
                (row.account_pool_id, row.workspace_id): row for row in saved.scalars().all()
            }
        token_data = [
            (key, self._entry_token(
                entries.get(email), team_space_id,
                workspaces.get((entries[email].id, team_space_id)) if email in entries else None,
            ))
            for key, email, team_space_id in normalized
        ]
        missing = [(email, space) for (_, email, space), (_, token)
                   in zip(normalized, token_data) if space and not token]
        if missing:
            records = (await db.execute(select(MemberAuthorization).where(
                MemberAuthorization.email.in_({email for email, _ in missing}),
                MemberAuthorization.account_id.in_({space for _, space in missing}),
            ))).scalars().all()
            auth_by_key = {(record.email, record.account_id): record for record in records}
            token_data = [
                (key, token or self._authorization_token(auth_by_key.get((email, space)), space))
                for (key, email, space), (_, token) in zip(normalized, token_data)
            ]
        return token_data

    async def check_many(
        self,
        db: AsyncSession,
        accounts: list[str | tuple[str, str]],
    ) -> dict[Any, dict[str, Any]]:
        normalized = self._normalize_accounts(accounts)
        if not normalized:
            return {}
        token_data = await self._tokens_for_accounts(db, normalized)
        proxies = await self._proxies(db)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def check(key: Any, credentials: tuple[str, str] | None):
            async with semaphore:
                return key, await self._check_token(credentials, proxies)

        values = await asyncio.gather(*(check(key, credentials) for key, credentials in token_data))
        return dict(values)


account_pool_usage_service = AccountPoolUsageService()
