"""Team-level auto-kick hours, per-member participation and due-member scanning."""
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RotationAction, Team, TeamEmailMapping
from app.services.member_auto_kick_policy import (
    MAX_AUTO_KICK_HOURS, MIN_AUTO_KICK_HOURS, OWNER_ROLE, auto_kick_enabled_condition, participates,
    record_failure, refresh_deadline, set_participation, team_auto_kick_hours,
)
from app.utils.time_utils import get_now
from app.services.warranty_member_protection import (
    is_warranty_member, protect_mapping, protect_team_mappings, warranty_member_condition,
)

logger = logging.getLogger(__name__)

JOINED_STATUS = "joined"
OPEN_ROTATION_STATUSES = ("pending", "executing", "reconciling")


@dataclass(frozen=True)
class DueMember:
    team_id: int
    user_id: str
    email: str


DeleteMember = Callable[..., Awaitable[Dict[str, object]]]
CompleteReplacement = Callable[[int, str, AsyncSession], Awaitable[str]]


def schedule_payload(mapping) -> Dict[str, object]:
    enabled = participates(mapping)
    return {
        "auto_kick_enabled": enabled,
        "auto_kick_at": mapping.auto_kick_at.isoformat() if enabled and mapping.auto_kick_at else None,
        "auto_kick_failures": int(mapping.auto_kick_failures or 0) if enabled else 0,
        "auto_kick_last_error": mapping.auto_kick_last_error if enabled else None,
        "auto_kick_retry_at": (mapping.auto_kick_retry_at.isoformat()
                               if enabled and mapping.auto_kick_retry_at else None),
    }


class MemberAutoKickService:
    async def update_team_hours(self, team_id: int, hours: int, db_session: AsyncSession) -> Dict[str, object]:
        team = await db_session.get(Team, team_id)
        if not team:
            return {"success": False, "error": "Team 不存在"}
        if not MIN_AUTO_KICK_HOURS <= hours <= MAX_AUTO_KICK_HOURS:
            return {"success": False,
                    "error": f"自动下线时长必须在 {MIN_AUTO_KICK_HOURS} 到 {MAX_AUTO_KICK_HOURS} 小时之间"}
        team.member_auto_kick_hours = hours
        mappings = await self._joined_mappings(team_id, db_session)
        await protect_team_mappings(db_session, team_id, mappings)
        for mapping in mappings:
            refresh_deadline(mapping, hours)
        await db_session.commit()
        return {"success": True, "hours": hours,
                "message": f"已设置为加入 {hours} 小时后自动下线，仅对参与自动下线的成员生效"}

    async def update_member_participation(
        self, team_id: int, user_id: str, enabled: bool, db_session: AsyncSession,
    ) -> Dict[str, object]:
        team = await db_session.get(Team, team_id)
        if not team:
            return {"success": False, "error": "Team 不存在"}
        mapping = (await db_session.execute(select(TeamEmailMapping).where(
            TeamEmailMapping.team_id == team_id,
            TeamEmailMapping.upstream_user_id == user_id,
            TeamEmailMapping.status == JOINED_STATUS,
        ))).scalar_one_or_none()
        if not mapping:
            return {"success": False, "error": "该成员已不在当前 Team 中，请刷新列表"}
        if mapping.member_role == OWNER_ROLE:
            return {"success": False, "error": "Team 所有者不会自动下线，无需设置"}
        if await is_warranty_member(db_session, team_id, mapping.email):
            protect_mapping(mapping)
            await db_session.commit()
            return {"success": not enabled, "message": "质保兑换成员不会自动下线",
                    "error": "质保兑换成员不参与自动踢出，不能开启自动下线" if enabled else None,
                    **schedule_payload(mapping)}
        set_participation(mapping, enabled, team_auto_kick_hours(team))
        await db_session.commit()
        return {"success": True,
                "message": "该成员将按 Team 时长自动下线" if enabled else "该成员不会自动下线",
                **schedule_payload(mapping)}

    async def run_due_members(
        self,
        db_session: AsyncSession,
        delete_member: DeleteMember,
    ) -> Dict[str, int | bool]:
        due_members = await self._due_members(db_session)
        stats: Dict[str, int | bool] = {
            "success": True,
            "scanned": len(due_members),
            "kicked": 0,
            "failed": 0,
        }
        for member in due_members:
            try:
                if await is_warranty_member(db_session, member.team_id, member.email):
                    continue
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
                retry_at = await self._record_failure(member, result.get("error"), db_session)
                logger.warning(
                    "成员自动下线失败: team=%s email=%s error=%s retry_at=%s",
                    member.team_id, member.email, result.get("error"), retry_at,
                )
                stats["failed"] = int(stats["failed"]) + 1
                continue

            stats["kicked"] = int(stats["kicked"]) + 1
            logger.info("成员到期下线已确认: team=%s email=%s method=%s",
                        member.team_id, member.email, result.get("removal_method"))

        stats["success"] = stats["failed"] == 0
        return stats

    @staticmethod
    async def _record_failure(member: DueMember, error, db_session: AsyncSession):
        mapping = (await db_session.execute(select(TeamEmailMapping).where(
            TeamEmailMapping.team_id == member.team_id,
            TeamEmailMapping.email == member.email,
        ).execution_options(populate_existing=True))).scalar_one_or_none()
        now = get_now()
        if mapping and await is_warranty_member(db_session, member.team_id, member.email):
            protect_mapping(mapping)
            await db_session.commit()
            return None
        # 计划在执行期间被修改或取消时，不把旧计划的失败记到新计划上。
        if not (mapping and mapping.status == JOINED_STATUS and mapping.upstream_user_id == member.user_id
                and mapping.auto_kick_at and mapping.auto_kick_at <= now):
            return None
        record_failure(mapping, error, now)
        await db_session.commit()
        return mapping.auto_kick_retry_at

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
            "exported": 0, "waiting": 0, "cancelled": 0, "failed": 0,
        }
        for mapping_id in pending_ids:
            mapping = await db_session.get(TeamEmailMapping, mapping_id)
            team_id, email = mapping.team_id, mapping.email
            try:
                # removed 由明确退组或多次完整同步确认；单次上游缺失和读取失败
                # 不足以取消任务，invited/joined 仍按原流程等待或重试。
                if mapping.status == "removed":
                    mapping.replacement_export_pending = False
                    await db_session.commit()
                    stats["cancelled"] += 1
                    logger.info("取消已离组账号的补位导入: team=%s email=%s", team_id, email)
                    continue
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
    async def _joined_mappings(team_id: int, db_session: AsyncSession) -> list[TeamEmailMapping]:
        return list((await db_session.execute(select(TeamEmailMapping).where(
            TeamEmailMapping.team_id == team_id,
            TeamEmailMapping.status == JOINED_STATUS,
        ))).scalars().all())

    async def _due_members(self, db_session: AsyncSession) -> list[DueMember]:
        now = get_now()
        result = await db_session.execute(
            select(TeamEmailMapping)
            .where(
                TeamEmailMapping.status == JOINED_STATUS,
                TeamEmailMapping.auto_kick_at <= now,
                or_(TeamEmailMapping.auto_kick_retry_at.is_(None), TeamEmailMapping.auto_kick_retry_at <= now),
                ~exists().where(
                    RotationAction.team_id == TeamEmailMapping.team_id,
                    RotationAction.email == TeamEmailMapping.email,
                    RotationAction.status.in_(OPEN_ROTATION_STATUSES),
                ),
                auto_kick_enabled_condition(),
                ~warranty_member_condition(TeamEmailMapping.team_id, TeamEmailMapping.email),
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
            )
            for mapping in result.scalars().all()
        ]


member_auto_kick_service = MemberAutoKickService()
