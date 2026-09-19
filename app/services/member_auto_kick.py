"""Per-Team member auto-kick settings and due-member scanning."""
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, TeamEmailMapping
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

DEFAULT_MEMBER_AUTO_KICK_HOURS = 2
MIN_MEMBER_AUTO_KICK_HOURS = 1
MAX_MEMBER_AUTO_KICK_HOURS = 8760
OWNER_ROLE = "account-owner"
JOINED_STATUS = "joined"


@dataclass(frozen=True)
class DueMember:
    team_id: int
    user_id: str
    email: str


DeleteMember = Callable[..., Awaitable[Dict[str, object]]]
InviteReplacement = Callable[..., Awaitable[Dict[str, object]]]


class MemberAutoKickService:
    @staticmethod
    def validate_hours(hours: int) -> int:
        value = int(hours)
        if not MIN_MEMBER_AUTO_KICK_HOURS <= value <= MAX_MEMBER_AUTO_KICK_HOURS:
            raise ValueError(
                f"自动踢人时长必须在 {MIN_MEMBER_AUTO_KICK_HOURS} 到 "
                f"{MAX_MEMBER_AUTO_KICK_HOURS} 小时之间"
            )
        return value

    @staticmethod
    def calculate_deadline(joined_at, role: Optional[str], hours: int):
        if not joined_at or role == OWNER_ROLE:
            return None
        return joined_at + timedelta(hours=hours)

    async def update_team_hours(
        self,
        team_id: int,
        hours: int,
        db_session: AsyncSession,
    ) -> Dict[str, object]:
        team = await db_session.get(Team, team_id)
        if not team:
            return {"success": False, "error": "Team 不存在"}

        normalized_hours = self.validate_hours(hours)
        team.member_auto_kick_hours = normalized_hours
        mappings = await self._joined_mappings(team_id, db_session)
        for mapping in mappings:
            mapping.auto_kick_at = self.calculate_deadline(
                mapping.joined_at,
                mapping.member_role,
                normalized_hours,
            )
        await db_session.commit()
        return {
            "success": True,
            "message": f"已设置为成员加入 {normalized_hours} 小时后自动踢出",
            "hours": normalized_hours,
        }

    async def run_due_members(
        self,
        db_session: AsyncSession,
        delete_member: DeleteMember,
        *,
        invite_replacement: InviteReplacement,
    ) -> Dict[str, int | bool]:
        due_members = await self._due_members(db_session)
        stats: Dict[str, int | bool] = {
            "success": True,
            "scanned": len(due_members),
            "kicked": 0,
            "failed": 0,
            "replacement_invited": 0,
            "replacement_unavailable": 0,
            "replacement_failed": 0,
            "replacement_pending": 0,
        }
        for member in due_members:
            try:
                result = await delete_member(
                    member.team_id,
                    member.user_id,
                    db_session,
                    email=member.email,
                )
            except Exception:
                await db_session.rollback()
                logger.exception(
                    "成员自动踢出异常: team=%s email=%s",
                    member.team_id,
                    member.email,
                )
                result = {"success": False}

            if not result.get("success"):
                stats["failed"] = int(stats["failed"]) + 1
                continue

            stats["kicked"] = int(stats["kicked"]) + 1
            queued = await self._queue_replacement(member.team_id, db_session)
            if not queued:
                stats["replacement_failed"] = int(stats["replacement_failed"]) + 1

        await self._process_pending_replacements(
            db_session=db_session,
            invite_replacement=invite_replacement,
            stats=stats,
        )
        stats["replacement_pending"] = await self._pending_replacement_count(db_session)

        stats["success"] = stats["failed"] == 0 and stats["replacement_failed"] == 0
        return stats

    @staticmethod
    async def _queue_replacement(team_id, db_session) -> bool:
        team = await db_session.get(Team, team_id)
        if not team:
            return False
        team.pending_replacements = int(team.pending_replacements or 0) + 1
        await db_session.commit()
        return True

    async def _process_pending_replacements(
        self,
        *,
        db_session,
        invite_replacement,
        stats,
    ) -> None:
        result = await db_session.execute(
            select(Team)
            .where(Team.pending_replacements > 0)
            .order_by(Team.id.asc())
        )
        for team in result.scalars().all():
            await self._fill_team_replacements(
                team,
                db_session=db_session,
                invite_replacement=invite_replacement,
                stats=stats,
            )

    async def _fill_team_replacements(
        self,
        team,
        *,
        db_session,
        invite_replacement,
        stats,
    ) -> None:
        while int(team.pending_replacements or 0) > 0:
            replacement = await self._invite_replacement(
                team.id,
                db_session,
                invite_replacement,
            )
            status = replacement.get("status")
            if status == "no_candidate":
                stats["replacement_unavailable"] += int(team.pending_replacements)
                return
            if status != "invited":
                stats["replacement_failed"] += 1
                return
            team.pending_replacements -= 1
            stats["replacement_invited"] += 1
            await db_session.commit()

    @staticmethod
    async def _pending_replacement_count(db_session) -> int:
        result = await db_session.execute(
            select(Team.pending_replacements).where(Team.pending_replacements > 0)
        )
        return sum(int(value or 0) for value in result.scalars().all())

    @staticmethod
    async def _invite_replacement(team_id, db_session, invite_replacement):
        try:
            return await invite_replacement(team_id, db_session)
        except Exception:
            await db_session.rollback()
            logger.exception("成员自动补位异常: team=%s", team_id)
            return {"success": False, "status": "failed"}

    async def _joined_mappings(
        self,
        team_id: int,
        db_session: AsyncSession,
    ) -> list[TeamEmailMapping]:
        result = await db_session.execute(
            select(TeamEmailMapping).where(
                TeamEmailMapping.team_id == team_id,
                TeamEmailMapping.status == JOINED_STATUS,
            )
        )
        return list(result.scalars().all())

    async def _due_members(self, db_session: AsyncSession) -> list[DueMember]:
        result = await db_session.execute(
            select(TeamEmailMapping)
            .join(Team, TeamEmailMapping.team_id == Team.id)
            .where(
                TeamEmailMapping.status == JOINED_STATUS,
                TeamEmailMapping.auto_kick_at.is_not(None),
                TeamEmailMapping.auto_kick_at <= get_now(),
                TeamEmailMapping.upstream_user_id.is_not(None),
                TeamEmailMapping.member_role != OWNER_ROLE,
            )
            .order_by(TeamEmailMapping.auto_kick_at.asc())
        )
        return [
            DueMember(mapping.team_id, mapping.upstream_user_id, mapping.email)
            for mapping in result.scalars().all()
        ]


member_auto_kick_service = MemberAutoKickService()
