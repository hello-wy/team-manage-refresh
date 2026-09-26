"""Selected account-pool exports, rotations, and deletes."""
import json
import logging
from uuid import uuid4

from sqlalchemy import func, select

from app.models import AccountPoolEntry, AccountPoolExportJob, AccountPoolTotpJob, AccountPoolWorkspace
from app.services.account_pool_export import selected_workspace
from app.services.encryption import encryption_service
from app.services.sub2api_export_records import sub2api_export_record_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


class AccountPoolBatchService:
    def __init__(self, sessions, totp, cipher):
        self._sessions = sessions
        self._totp = totp
        self._cipher = cipher

    @staticmethod
    async def entries(session, ids):
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("请选择有效且不重复的账号")
        rows = (await session.execute(select(AccountPoolEntry).where(
            AccountPoolEntry.id.in_(ids), AccountPoolEntry.deleted_at.is_(None)
        ))).scalars().all()
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(ids):
            raise ValueError("所选账号不存在或已删除，请刷新列表")
        return [by_id[item_id] for item_id in ids]

    async def merged_json(self, session, ids):
        entries = await self.entries(session, ids)
        snapshots = (await session.execute(select(AccountPoolWorkspace).where(
            AccountPoolWorkspace.account_pool_id.in_(ids),
            AccountPoolWorkspace.export_json_encrypted.is_not(None)
        ))).scalars().all()
        grouped = {entry.id: [] for entry in entries}
        for snapshot in snapshots:
            grouped[snapshot.account_pool_id].append(snapshot.export_json_encrypted)
        accounts = []
        for entry in entries:
            encrypted = grouped[entry.id] or [entry.export_json_encrypted]
            if not encrypted[0]:
                raise ValueError(f"{entry.email} 没有已保存的 JSON，请先刷新空间")
            for value in encrypted:
                payload = json.loads(self._cipher.decrypt_token(value))
                if (payload.get("type") != "sub2api-data"
                        or not isinstance(payload.get("accounts"), list)
                        or not payload["accounts"]):
                    raise ValueError(f"{entry.email} 的 JSON 格式无效")
                accounts.extend(payload["accounts"])
        if not accounts:
            raise ValueError("所选账号的 JSON 没有账户数据")
        return {"type": "sub2api-data", "version": 1,
                "exported_at": get_now().isoformat(), "proxies": [], "accounts": accounts}

    async def enqueue_exports(self, session, ids):
        entries = await self.entries(session, ids)
        snapshots = (await session.execute(select(AccountPoolWorkspace).where(
            AccountPoolWorkspace.account_pool_id.in_(ids),
            AccountPoolWorkspace.export_json_encrypted.is_not(None)
        ))).scalars().all()
        grouped = {entry.id: [] for entry in entries}
        for snapshot in snapshots:
            grouped[snapshot.account_pool_id].append(snapshot.workspace_id)
        jobs = [AccountPoolExportJob(account_pool_id=entry.id,
                                     workspace_id=selected_workspace(entry, workspace_id),
                                     status="pending")
                for entry in entries for workspace_id in (grouped[entry.id] or [""])]
        session.add_all(jobs)
        await session.commit()
        return jobs

    async def enqueue_rotations(self, session, ids):
        entries = await self.entries(session, ids)
        batch_id = str(uuid4())
        jobs = [AccountPoolTotpJob(batch_id=batch_id, account_pool_id=entry.id,
                                   email=entry.email, status="pending") for entry in entries]
        session.add_all(jobs)
        await session.commit()
        return batch_id

    async def run_rotations(self, batch_id):
        async with self._sessions() as session:
            ids = (await session.execute(select(AccountPoolTotpJob.id).where(
                AccountPoolTotpJob.batch_id == batch_id
            ).order_by(AccountPoolTotpJob.id))).scalars().all()
        for job_id in ids:
            await self._run_rotation(job_id)

    async def _run_rotation(self, job_id):
        async with self._sessions() as session:
            job = await session.get(AccountPoolTotpJob, job_id)
            job.status = "running"
            await session.commit()
            try:
                secret = await self._totp.rotate(session, job.account_pool_id)
            except Exception as exc:
                await session.rollback()
                job = await session.get(AccountPoolTotpJob, job_id)
                job.status = "failed"
                job.error = str(exc)
                if getattr(exc, "new_secret", ""):
                    job.new_secret_encrypted = self._cipher.encrypt_token(exc.new_secret)
                logger.exception("账号池 2FA 更换失败: email=%s job=%s result=%s",
                                 job.email, job.id, job.error)
            else:
                job = await session.get(AccountPoolTotpJob, job_id)
                job.status = "completed"
                job.new_secret_encrypted = self._cipher.encrypt_token(secret)
                logger.info("账号池 2FA 更换成功: email=%s job=%s result=completed",
                            job.email, job.id)
            job.finished_at = get_now()
            await session.commit()

    async def rotation_status(self, session, batch_id):
        jobs = (await session.execute(select(AccountPoolTotpJob).where(
            AccountPoolTotpJob.batch_id == batch_id
        ).order_by(AccountPoolTotpJob.id))).scalars().all()
        if not jobs:
            raise ValueError("2FA 任务不存在")
        return [{"email": job.email, "status": job.status, "error": job.error,
                 "two_factor_secret": self._cipher.decrypt_token(job.new_secret_encrypted)
                 if job.new_secret_encrypted else None} for job in jobs]

    async def rotation_history(self, session, page, page_size):
        total = (await session.execute(select(func.count()).select_from(AccountPoolTotpJob))).scalar_one()
        jobs = (await session.execute(select(AccountPoolTotpJob).order_by(
            AccountPoolTotpJob.id.desc()
        ).offset((page - 1) * page_size).limit(page_size))).scalars().all()
        return total, [{"email": job.email, "status": job.status,
                        "error": job.error, "batch_id": job.batch_id,
                        "created_at": job.created_at.isoformat(),
                        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                        "two_factor_secret": self._cipher.decrypt_token(job.new_secret_encrypted)
                        if job.new_secret_encrypted else None} for job in jobs]

    async def delete(self, session, ids):
        entries = await self.entries(session, ids)
        active_rotations = (await session.execute(select(AccountPoolTotpJob.id).where(
            AccountPoolTotpJob.account_pool_id.in_(ids),
            AccountPoolTotpJob.status.in_(("pending", "running"))
        ))).first()
        active_exports = (await session.execute(select(AccountPoolExportJob.id).where(
            AccountPoolExportJob.account_pool_id.in_(ids),
            AccountPoolExportJob.status.in_(("pending", "running"))
        ))).first()
        if active_rotations or active_exports:
            raise ValueError("所选账号存在后台任务，请等待任务结束后删除")
        await sub2api_export_record_service.delete_for_emails(
            session, [entry.email for entry in entries]
        )
        for entry in entries:
            await session.delete(entry)
        await session.commit()
        return [entry.email for entry in entries]


def create_account_pool_batch_service(sessions, totp):
    return AccountPoolBatchService(sessions, totp, encryption_service)
