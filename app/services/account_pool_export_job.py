"""Shared workspace validation, credential recovery and background export lifecycle."""
import json
import logging

from app.models import AccountPoolEntry
from app.services.account_pool_authorization import AccountPoolAuthorizationError
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


def selected_workspace(entry: AccountPoolEntry, requested_id: str) -> str:
    workspace_id = requested_id.strip() or str(entry.workspace_id or "").strip()
    scan = json.loads(entry.workspace_state_json) if entry.workspace_state_json else {}
    available = {str(item.get("id") or "") for item in scan.get("available_workspaces") or []}
    if workspace_id and available and workspace_id not in available:
        raise AccountPoolAuthorizationError("所选 Team 不在该账号最近扫描的空间列表中")
    if workspace_id and not available and workspace_id != str(entry.workspace_id or "").strip():
        raise AccountPoolAuthorizationError("请先扫描该账号的 Team 空间")
    if not workspace_id and available:
        raise AccountPoolAuthorizationError("请先选择要导出的 Team")
    return workspace_id


class AccountPoolExportJobService:
    def __init__(self, sessions, authorization):
        self._sessions = sessions
        self._authorization = authorization

    async def enqueue(self, session, entry_id: int, workspace_id: str):
        entry = await session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            raise AccountPoolAuthorizationError("账号不存在")
        target = selected_workspace(entry, workspace_id)
        job = self.job_model(account_pool_id=entry_id, workspace_id=target, status="pending")
        session.add(job)
        await session.commit()
        return job

    async def run(self, job_id: int) -> None:
        async with self._sessions() as session:
            job = await session.get(self.job_model, job_id)
            job.status = "running"
            await session.commit()
            try:
                result = await self._login_and_save(session, job)
                await self._import(session, job, result)
                job.status = "completed"
                job.finished_at = get_now()
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.exception("账号池后台导出失败: table=%s job=%s", self.job_model.__tablename__, job_id)
                job = await session.get(self.job_model, job_id)
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = get_now()
                await session.commit()

    async def _login_and_save(self, session, job):
        current = await session.get(AccountPoolEntry, job.account_pool_id)
        if current is None or current.deleted_at is not None:
            raise AccountPoolAuthorizationError("账号不存在")
        version = (current.password_encrypted, current.two_factor_secret_encrypted)
        result = await self._authorization.login_entry(session, job.account_pool_id, job.workspace_id)
        saved = await self._authorization.save_result(
            session, job.account_pool_id, result,
            update_current=current.workspace_id == job.workspace_id,
            credential_version=version,
            liveness=("alive", "密码、2FA 与 OAuth 登录验证通过"),
        )
        if not saved:
            raise AccountPoolAuthorizationError("登录期间账号凭据已变更，请重新登录")
        return result

    async def _import(self, session, job, result):
        raise NotImplementedError
