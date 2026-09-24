"""Persist and execute account-pool Sub2API exports after the HTTP response."""
from __future__ import annotations

import json
import logging

from app.models import AccountPoolEntry, AccountPoolExportJob, Team
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


class AccountPoolExportService:
    def __init__(self, sessions, authorization, sub2api, records):
        self._sessions = sessions
        self._authorization = authorization
        self._sub2api = sub2api
        self._records = records

    async def enqueue(self, session, entry_id: int, workspace_id: str) -> AccountPoolExportJob:
        entry = await session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            raise AccountPoolAuthorizationError("账号不存在")
        target = selected_workspace(entry, workspace_id)
        job = AccountPoolExportJob(account_pool_id=entry_id, workspace_id=target, status="pending")
        session.add(job)
        await session.commit()
        return job

    async def run(self, job_id: int) -> None:
        async with self._sessions() as session:
            job = await session.get(AccountPoolExportJob, job_id)
            job.status = "running"
            await session.commit()
            try:
                await self._export(session, job)
            except Exception as exc:
                await session.rollback()
                logger.exception("账号池后台导出失败: job=%s", job_id)
                job = await session.get(AccountPoolExportJob, job_id)
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = get_now()
                await session.commit()

    async def _export(self, session, job) -> None:
        result = await self._authorization.login_entry(
            session, job.account_pool_id, job.workspace_id
        )
        current = await session.get(AccountPoolEntry, job.account_pool_id)
        await self._authorization.save_result(
            session, job.account_pool_id, result,
            update_current=current.workspace_id == job.workspace_id,
        )
        imported = await self._sub2api.import_member(result.payload, session)
        entry = await session.get(AccountPoolEntry, job.account_pool_id)
        actual_id = str(result.workspace.get("workspace_id") or job.workspace_id).strip()
        team = await self._team(session, actual_id)
        await self._records.record_success(
            session, email=entry.email, team_space_id=actual_id,
            team_id=team.id if team else None,
            team_name=team.team_name if team else result.workspace.get("workspace_name"),
            team_email=team.email if team else None,
            sub2api_account_id=imported["account_id"],
        )
        job.status = "completed"
        job.workspace_id = actual_id
        job.sub2api_account_id = imported["account_id"]
        job.finished_at = get_now()
        await session.commit()

    @staticmethod
    async def _team(session, workspace_id):
        from sqlalchemy import select
        return (await session.execute(
            select(Team).where(Team.account_id == workspace_id).order_by(Team.id)
        )).scalars().first()
