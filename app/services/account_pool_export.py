"""Sub2API-specific account creation and successful export records."""
from app.models import AccountPoolEntry, AccountPoolExportJob, Team
from app.services.account_pool_export_job import AccountPoolExportJobService, selected_workspace


class AccountPoolExportService(AccountPoolExportJobService):
    job_model = AccountPoolExportJob

    def __init__(self, sessions, authorization, *, sub2api, records):
        super().__init__(sessions, authorization)
        self._sub2api = sub2api
        self._records = records

    async def _import(self, session, job, result):
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
        job.workspace_id = actual_id
        job.sub2api_account_id = imported["account_id"]

    @staticmethod
    async def _team(session, workspace_id):
        from sqlalchemy import select
        return (await session.execute(
            select(Team).where(Team.account_id == workspace_id).order_by(Team.id)
        )).scalars().first()
