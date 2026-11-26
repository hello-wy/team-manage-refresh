"""CPA destinations reuse the same credential preparation as Sub2API exports."""
from sqlalchemy import func, select

from app.cpa_export_model import CpaExportJob
from app.services.account_pool_batch import AccountPoolBatchService, batch_export_targets
from app.services.account_pool_export_job import AccountPoolExportJobService


class TeamCpaExportService:
    def __init__(self, *, preparation, cpa):
        self._preparation = preparation
        self._cpa = cpa

    async def push(self, team_id, db):
        prepared = await self._preparation.prepare(team_id, db)
        imported = await self._cpa.import_member(prepared.payload, db)
        return {"team_id": team_id, **imported, "relogin_attempted": prepared.relogin_attempted}


class AccountPoolCpaExportService(AccountPoolExportJobService):
    job_model = CpaExportJob

    def __init__(self, sessions, authorization, *, cpa):
        super().__init__(sessions, authorization)
        self._cpa = cpa

    async def enqueue_batch(self, session, ids):
        entries = await AccountPoolBatchService.entries(session, list(dict.fromkeys(ids)))
        targets = await batch_export_targets(session, entries)
        jobs = [CpaExportJob(account_pool_id=entry.id, workspace_id=workspace_id, status="pending")
                for entry, workspace_id in targets]
        session.add_all(jobs)
        await session.flush()
        queued = [{"id": job.id, "email": entry.email, "workspace_id": workspace_id}
                  for job, (entry, workspace_id) in zip(jobs, targets)]
        await session.commit()
        return queued

    async def run_batch(self, job_ids):
        for job_id in job_ids:
            await self.run(job_id)

    async def _import(self, session, job, result):
        imported = await self._cpa.import_member(result.payload, session)
        job.workspace_id = str(result.workspace.get("workspace_id") or job.workspace_id).strip()
        job.filename = imported["filename"]


async def attach_cpa_export_jobs(db, entries):
    if not entries:
        return entries
    latest_ids = select(func.max(CpaExportJob.id)).where(
        CpaExportJob.account_pool_id.in_([entry["id"] for entry in entries])
    ).group_by(CpaExportJob.account_pool_id)
    jobs = (await db.execute(select(CpaExportJob).where(CpaExportJob.id.in_(latest_ids)))).scalars()
    latest = {job.account_pool_id: job for job in jobs}
    return [{**entry, "cpa_export_job": latest.get(entry["id"])} for entry in entries]
