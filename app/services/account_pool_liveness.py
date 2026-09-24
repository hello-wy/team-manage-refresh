"""Scheduled OAuth verification and workspace refresh for account-pool entries."""
import json
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AccountPoolEntry
from app.services.account_pool_authorization import (
    AccountPoolAuthorizationError,
    AccountPoolAuthorizationService,
)
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)
INVALID_ACCOUNT_CODES = frozenset({"invalid_username_or_password", "account_deactivated"})


class AccountPoolLivenessService:
    def __init__(self, credential_service, authorization_service: AccountPoolAuthorizationService):
        self._credentials = credential_service
        self._authorization = authorization_service

    async def check_entry(self, session: AsyncSession, entry_id: int) -> str | None:
        entry = await session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            return None
        version = (entry.password_encrypted, entry.two_factor_secret_encrypted)
        credentials = await self._credentials.get_credentials(session, entry.email)
        if not credentials or not credentials["password"]:
            return await self._save_failure(session, entry_id, version, "missing", "未保存登录密码")
        try:
            return await self._refresh_workspaces(session, entry, version)
        except AccountPoolAuthorizationError as exc:
            message = str(exc)
            status = "invalid" if any(code in message for code in INVALID_ACCOUNT_CODES) else "error"
            return await self._save_failure(session, entry_id, version, status, message)
        except Exception:
            logger.exception("账号验活异常: entry_id=%s", entry_id)
            return await self._save_failure(
                session,
                entry_id,
                version,
                "error",
                "检测请求异常，请查看服务端日志",
            )

    async def _refresh_workspaces(self, session, entry, version) -> str | None:
        scan = await self._authorization.scan_entry(session, entry.id)
        options = [item for item in scan.get("available_workspaces", []) if item.get("id")]
        if not options:
            options = [{"id": scan.get("workspace_id") or "", "is_personal": False}]
        ids = [str(item["id"]) for item in options]
        selected = entry.workspace_id if entry.workspace_id in ids else next(
            (str(item["id"]) for item in options if not item.get("is_personal")), ids[0]
        )
        ordered = [selected, *(workspace_id for workspace_id in ids if workspace_id != selected)]
        for index, workspace_id in enumerate(ordered):
            result = await self._authorization.login_entry(session, entry.id, workspace_id)
            saved = await self._authorization.save_result(
                session, entry.id, result, credential_version=version,
                liveness=("alive", "密码、2FA 与 OAuth 登录验证通过") if index == 0 else None,
                update_current=index == 0, workspace_state=scan,
            )
            if not saved:
                return None
        return "alive"

    async def _save_failure(
        self,
        session: AsyncSession,
        entry_id: int,
        version: tuple[str | None, str | None],
        status: str,
        message: str,
    ) -> str | None:
        now = get_now()
        state = {"status": "workspace_error", "error": message}
        entry = await session.get(AccountPoolEntry, entry_id)
        saved = await session.execute(
            update(AccountPoolEntry)
            .where(
                AccountPoolEntry.id == entry_id,
                AccountPoolEntry.deleted_at.is_(None),
                AccountPoolEntry.password_encrypted == version[0],
                AccountPoolEntry.two_factor_secret_encrypted == version[1],
            )
            .values(
                liveness_status=status,
                liveness_message=message,
                liveness_checked_at=now,
                workspace_status="workspace_error",
                workspace_checked_at=now,
                workspace_state_json=(entry.workspace_state_json if entry and entry.workspace_state_json
                                      else json.dumps(state, ensure_ascii=False)),
            )
        )
        await session.commit()
        return status if saved.rowcount else None

    async def check_all(self, sessions: async_sessionmaker) -> dict[str, int]:
        async with sessions() as session:
            entry_ids = list((await session.scalars(
                select(AccountPoolEntry.id)
                .where(AccountPoolEntry.deleted_at.is_(None))
                .order_by(AccountPoolEntry.id)
            )).all())
        counts = {"alive": 0, "invalid": 0, "error": 0, "missing": 0}
        for entry_id in entry_ids:
            async with sessions() as session:
                status = await self.check_entry(session, entry_id)
            if status is not None:
                counts[status] += 1
        return counts
