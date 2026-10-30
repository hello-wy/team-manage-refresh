"""Recover owner credentials before creating and recording a Sub2API account."""
from dataclasses import dataclass
from typing import Any

from app.models import Team
from app.services.member_authorization import MemberAuthorizationError


@dataclass(frozen=True)
class TeamSub2apiExportDependencies:
    recovery: Any
    authorization: Any
    sub2api: Any
    records: Any


@dataclass(frozen=True)
class TeamOwnerExport:
    team: Any
    payload: dict
    relogin_attempted: bool


class TeamSub2apiExportService:
    def __init__(self, dependencies: TeamSub2apiExportDependencies):
        self._deps = dependencies

    async def prepare(self, team_id, db):
        team = await db.get(Team, team_id)
        if team is None:
            raise MemberAuthorizationError("Team 不存在")
        workspace_id = team.account_id
        recovery = await self._deps.recovery.refresh(team_id, db, require_refresh_token=True)
        if not recovery.get("success"):
            raise MemberAuthorizationError(recovery.get("error") or "Team 凭证检查失败")
        if team.account_id != workspace_id:
            raise MemberAuthorizationError("Team 工作区已变化，未推送，请核对目标 Team")
        email = team.email.strip().lower()
        payload = await self._deps.authorization.export(team_id, email, db)
        return TeamOwnerExport(team, payload, bool(recovery.get("relogin_attempted")))

    async def push(self, team_id, db):
        prepared = await self.prepare(team_id, db)
        team, payload = prepared.team, prepared.payload
        email = team.email.strip().lower()
        imported = await self._deps.sub2api.import_member(payload, db)
        await self._deps.records.record_success(
            db, email=email, team_space_id=str(team.account_id or ""),
            team_id=team_id, team_name=team.team_name, team_email=team.email,
            sub2api_account_id=imported["account_id"],
        )
        await self._deps.authorization.mark_sub2api_exported(
            team_id, email, imported["account_id"], db
        )
        return {"team_id": team_id, "email": email, **imported,
                "relogin_attempted": prepared.relogin_attempted}
