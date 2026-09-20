"""Authorize invited replacement members and import their JSON into sub2api."""

from sqlalchemy import select

from app.models import TeamEmailMapping
from app.services.member_authorization import MemberAuthorizationError


class ReplacementExportService:
    def __init__(self, authorization_service, sub2api_service):
        self.authorization = authorization_service
        self.sub2api = sub2api_service

    async def complete(self, team_id, email, db_session):
        status = await self.authorization.check(team_id, email, db_session)
        if status["sub2api_exported"]:
            await self._clear_pending(team_id, email, db_session)
            return "exported"
        if not status["authorized"]:
            status = await self.authorization.automatic_login(
                team_id, email, db_session
            )
        if not status["authorized"]:
            raise MemberAuthorizationError(status["message"])
        if status["membership"] == "invited":
            return "waiting"
        if status["membership"] != "joined":
            raise MemberAuthorizationError(status["message"])
        payload = await self.authorization.export(team_id, email, db_session)
        result = await self.sub2api.import_member(payload, db_session)
        await self.authorization.mark_sub2api_exported(
            team_id, email, result["account_id"], db_session
        )
        await self._clear_pending(team_id, email, db_session)
        return "exported"

    @staticmethod
    async def _clear_pending(team_id, email, db_session):
        mapping = (await db_session.execute(
            select(TeamEmailMapping).where(
                TeamEmailMapping.team_id == team_id,
                TeamEmailMapping.email == email,
            )
        )).scalar_one_or_none()
        if mapping and mapping.replacement_export_pending:
            mapping.replacement_export_pending = False
            await db_session.commit()
