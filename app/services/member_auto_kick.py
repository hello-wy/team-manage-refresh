"""Per-Team member auto-kick settings and due-member scanning."""
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, TeamEmailMapping, TeamReplacementQueue
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
    seat_type: str


DeleteMember = Callable[..., Awaitable[Dict[str, object]]]
InviteReplacement = Callable[..., Awaitable[Dict[str, object]]]
CompleteReplacement = Callable[[int, str, AsyncSession], Awaitable[str]]


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
    def calculate_deadline(
        joined_at,
        role: Optional[str],
        hours: int,
        exempt: bool = False,
    ):
        if exempt or not joined_at or role == OWNER_ROLE:
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
                mapping.auto_kick_exempt,
            )
        await db_session.commit()
        return {
            "success": True,
            "message": f"已设置为成员加入 {normalized_hours} 小时后自动踢出",
            "hours": normalized_hours,
        }

    async def update_member_exemption(
        self,
        team_id: int,
        user_id: str,
        exempt: bool,
        db_session: AsyncSession,
    ) -> Dict[str, object]:
        team = await db_session.get(Team, team_id)
        if not team:
            return {"success": False, "error": "Team 不存在"}

        result = await db_session.execute(
            select(TeamEmailMapping).where(
                TeamEmailMapping.team_id == team_id,
                TeamEmailMapping.upstream_user_id == user_id,
                TeamEmailMapping.status == JOINED_STATUS,
            )
        )
        mapping = result.scalar_one_or_none()
        if not mapping:
            return {"success": False, "error": "该成员已不在当前 Team 中，请刷新列表"}
        if mapping.member_role == OWNER_ROLE:
            return {"success": False, "error": "Team 所有者不会自动下线，无需设置"}

        mapping.auto_kick_exempt = bool(exempt)
        hours = int(team.member_auto_kick_hours or DEFAULT_MEMBER_AUTO_KICK_HOURS)
        mapping.auto_kick_at = self.calculate_deadline(
            mapping.joined_at,
            mapping.member_role,
            hours,
            mapping.auto_kick_exempt,
        )
        await db_session.commit()
        return {
            "success": True,
            "exempt": mapping.auto_kick_exempt,
            "auto_kick_at": mapping.auto_kick_at.isoformat() if mapping.auto_kick_at else None,
            "message": "已设置该成员不会自动下线" if mapping.auto_kick_exempt else "已恢复该成员的自动下线计划",
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
            queued = await self._queue_replacement(
                member.team_id, member.seat_type, db_session
            )
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
    async def _queue_replacement(team_id, seat_type, db_session) -> bool:
        team = await db_session.get(Team, team_id)
        if not team:
            return False
        db_session.add(TeamReplacementQueue(team_id=team_id, seat_type=seat_type))
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
            .outerjoin(TeamReplacementQueue)
            .where(
                (Team.pending_replacements > 0)
                | TeamReplacementQueue.id.is_not(None)
            )
            .distinct()
            .order_by(Team.id.asc())
        )
        for team in result.scalars().all():
            await self._reconcile_replacement_queue(team, db_session)
            if not team.pending_replacements:
                continue
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
        while True:
            queue_item = await self._next_queue_item(team.id, db_session)
            if not queue_item:
                return
            replacement = await self._invite_replacement(
                team.id,
                queue_item.seat_type,
                db_session,
                invite_replacement,
            )
            status = replacement.get("status")
            if status == "no_candidate":
                stats["replacement_unavailable"] += int(team.pending_replacements or 0)
                return
            if status != "invited":
                stats["replacement_failed"] += 1
                self._log_replacement_failure(team, queue_item, replacement)
                return
            team.pending_replacements -= 1
            await db_session.delete(queue_item)
            stats["replacement_invited"] += 1
            await db_session.commit()

    @staticmethod
    async def _reconcile_replacement_queue(team, db_session) -> None:
        result = await db_session.execute(
            select(func.count(TeamReplacementQueue.id)).where(
                TeamReplacementQueue.team_id == team.id
            )
        )
        queue_count = int(result.scalar_one())
        pending_count = int(team.pending_replacements or 0)
        if pending_count == queue_count:
            return
        logger.error(
            "自动补位队列不一致，已按队列修复: team=%s name=%s pending=%s queue=%s",
            team.id,
            team.team_name,
            pending_count,
            queue_count,
        )
        team.pending_replacements = queue_count
        await db_session.commit()

    @staticmethod
    def _log_replacement_failure(team, queue_item, replacement) -> None:
        logger.warning(
            "自动补位邀请失败: team=%s name=%s queue_id=%s seat_type=%s "
            "email=%s status=%s error_code=%s status_code=%s error=%s",
            team.id,
            team.team_name,
            queue_item.id,
            queue_item.seat_type,
            replacement.get("email"),
            replacement.get("status"),
            replacement.get("error_code"),
            replacement.get("status_code"),
            replacement.get("error"),
        )

    @staticmethod
    async def _next_queue_item(team_id, db_session):
        result = await db_session.execute(
            select(TeamReplacementQueue)
            .where(TeamReplacementQueue.team_id == team_id)
            .order_by(TeamReplacementQueue.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _pending_replacement_count(db_session) -> int:
        result = await db_session.execute(
            select(func.count(TeamReplacementQueue.id))
        )
        return int(result.scalar_one())

    async def process_pending_exports(
        self, db_session: AsyncSession, complete: CompleteReplacement
    ) -> Dict[str, int | bool]:
        result = await db_session.execute(
            select(TeamEmailMapping.id)
            .where(TeamEmailMapping.replacement_export_pending.is_(True))
            .order_by(TeamEmailMapping.id.asc())
        )
        pending_ids = list(result.scalars().all())
        stats: Dict[str, int | bool] = {
            "success": True, "scanned": len(pending_ids),
            "exported": 0, "waiting": 0, "failed": 0,
        }
        for mapping_id in pending_ids:
            mapping = await db_session.get(TeamEmailMapping, mapping_id)
            team_id, email = mapping.team_id, mapping.email
            try:
                state = await complete(team_id, email, db_session)
                if state == "waiting":
                    stats["waiting"] += 1
                    continue
                if state != "exported":
                    raise ValueError(f"未知的补位导出结果: {state}")
                mapping.replacement_export_pending = False
                await db_session.commit()
                stats["exported"] += 1
            except Exception:
                await db_session.rollback()
                logger.exception(
                    "轮转账号自动授权或导入失败: team=%s email=%s",
                    team_id, email,
                )
                stats["failed"] += 1
        stats["success"] = stats["failed"] == 0
        return stats

    @staticmethod
    async def _invite_replacement(
        team_id, seat_type, db_session, invite_replacement
    ):
        try:
            return await invite_replacement(team_id, db_session, seat_type)
        except Exception:
            await db_session.rollback()
            logger.exception("成员自动补位异常: team=%s seat_type=%s", team_id, seat_type)
            return {
                "success": False,
                "status": "failed",
                "error_code": "replacement_exception",
                "error": "自动补位调用异常，请查看服务日志",
            }

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
                TeamEmailMapping.auto_kick_exempt.is_(False),
                TeamEmailMapping.upstream_user_id.is_not(None),
                TeamEmailMapping.member_role != OWNER_ROLE,
            )
            .order_by(TeamEmailMapping.auto_kick_at.asc())
        )
        return [
            DueMember(
                mapping.team_id,
                mapping.upstream_user_id,
                mapping.email,
                mapping.seat_type or "standard",
            )
            for mapping in result.scalars().all()
        ]


member_auto_kick_service = MemberAutoKickService()
