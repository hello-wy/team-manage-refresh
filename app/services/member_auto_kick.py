"""Team-level auto-kick hours, per-member participation and due-member scanning."""
import logging
import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RotationAction, Team, TeamEmailMapping, TeamReplacementQueue
from app.services.member_auto_kick_policy import (
    EXPIRY_POLICIES, MAX_AUTO_KICK_HOURS, MIN_AUTO_KICK_HOURS, OWNER_ROLE, auto_kick_enabled_condition, participates,
    record_failure, refresh_deadline, set_participation, team_auto_kick_hours,
)
from app.utils.time_utils import get_now
from app.services.warranty_member_protection import (
    is_warranty_member, protect_mapping, protect_team_mappings, warranty_member_condition,
    scheduled_departure_allowed,
)

logger = logging.getLogger(__name__)

JOINED_STATUS = "joined"
OPEN_ROTATION_STATUSES = ("pending", "executing", "reconciling")


@dataclass(frozen=True)
class DueMember:
    team_id: int
    user_id: str
    email: str
    mapping_id: int
    seat_type: str | None
    rotation_lane: str | None
    rotation_round: int


DeleteMember = Callable[..., Awaitable[Dict[str, object]]]
CompleteReplacement = Callable[[int, str, AsyncSession], Awaitable[str]]
ReplaceMember = Callable[[int, str, str, int, frozenset[int], AsyncSession], Awaitable[dict]]


def schedule_payload(mapping) -> Dict[str, object]:
    enabled = participates(mapping)
    policy = getattr(mapping, "departure_policy", None) or "timed"
    return {
        "joined_at": mapping.joined_at.isoformat() if mapping.joined_at else None,
        "auto_kick_enabled": enabled and policy not in EXPIRY_POLICIES,
        "departure_policy": policy,
        "usage_started_at": ((getattr(mapping, "usage_started_at", None) or mapping.joined_at).isoformat()
                             if (getattr(mapping, "usage_started_at", None) or mapping.joined_at) else None),
        "join_source": getattr(mapping, "join_source", None),
        "auto_kick_at": mapping.auto_kick_at.isoformat() if enabled and mapping.auto_kick_at else None,
        "auto_kick_failures": int(mapping.auto_kick_failures or 0) if enabled else 0,
        "auto_kick_last_error": mapping.auto_kick_last_error if enabled else None,
        "auto_kick_retry_at": (mapping.auto_kick_retry_at.isoformat()
                               if enabled and mapping.auto_kick_retry_at else None),
    }


class MemberAutoKickService:
    async def update_team_hours(
        self, team_id: int, hours: int, db_session: AsyncSession,
        rounds: int | None = None,
    ) -> Dict[str, object]:
        team = await db_session.get(Team, team_id)
        if not team:
            return {"success": False, "error": "Team 不存在"}
        if not MIN_AUTO_KICK_HOURS <= hours <= MAX_AUTO_KICK_HOURS:
            return {"success": False,
                    "error": f"自动下线时长必须在 {MIN_AUTO_KICK_HOURS} 到 {MAX_AUTO_KICK_HOURS} 小时之间"}
        if rounds is not None and not 0 <= rounds <= 1000:
            return {"success": False, "error": "每个高级席位轮次必须在 0 到 1000 之间，0 表示不限制"}
        team.member_auto_kick_hours = hours
        if rounds is not None:
            team.rotation_rounds = rounds
        mappings = await self._joined_mappings(team_id, db_session)
        await protect_team_mappings(db_session, team_id, mappings)
        for mapping in mappings:
            refresh_deadline(mapping, hours, team=team)
        await db_session.commit()
        return {"success": True, "hours": hours, "rounds": int(team.rotation_rounds or 0),
                "message": f"已设置为高级席位本次使用 {hours} 小时后自动下线，标准席位仍按 Team 到期时间下线"}

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
            protect_mapping(mapping, team)
            await db_session.commit()
            return {"success": not enabled, "message": "质保兑换成员仅在 Team 到期后下线",
                    "error": "质保兑换成员不参与小时级下线，不能修改使用规则" if enabled else None,
                    **schedule_payload(mapping)}
        if mapping.seat_type != "premium":
            return {"success": False, "error": "仅高级席位可设置小时级下线，标准席位按 Team 到期时间下线"}
        set_participation(mapping, enabled, team_auto_kick_hours(team), team=team)
        await db_session.commit()
        return {"success": True,
                "message": "该成员将按 Team 时长自动下线" if enabled else "该成员不会自动下线",
                "seat_type": mapping.seat_type,
                **schedule_payload(mapping)}

    async def run_due_members(
        self,
        db_session: AsyncSession,
        delete_member: DeleteMember,
        replace_member: ReplaceMember | None = None,
    ) -> Dict[str, int | bool]:
        due_members = await self._due_members(db_session)
        stats: Dict[str, int | bool] = {
            "success": True,
            "scanned": len(due_members),
            "kicked": 0,
            "failed": 0,
        }
        if replace_member:
            stats.update({"replacement_invited": 0, "replacement_waiting": 0,
                          "replacement_failed": 0, "rotation_finished": 0})
        for member in due_members:
            try:
                if not await scheduled_departure_allowed(db_session, member.team_id, member.email):
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
            if replace_member:
                replacement_status = await self._replace_after_departure(
                    member, db_session, replace_member,
                )
                key = {
                    "invited": "replacement_invited",
                    "waiting": "replacement_waiting",
                    "failed": "replacement_failed",
                    "finished": "rotation_finished",
                }.get(replacement_status)
                if key:
                    stats[key] = int(stats[key]) + 1

        stats["success"] = stats["failed"] == 0 and stats.get("replacement_failed", 0) == 0
        return stats

    async def _replace_after_departure(self, member, db_session, replace_member):
        """为已到期的高级席位创建并执行一次可重试的补位任务。"""
        if member.seat_type != "premium":
            return None
        team = await db_session.get(Team, member.team_id)
        if not team:
            return "failed"
        if team.rotation_mode != "auto":
            return None
        current_round = max(1, int(member.rotation_round or 1))
        max_rounds = max(0, int(team.rotation_rounds or 0))
        if max_rounds and current_round >= max_rounds:
            return "finished"
        lane_key = member.rotation_lane or f"mapping:{member.mapping_id}"
        next_round = current_round + 1
        existing = (await db_session.execute(select(TeamReplacementQueue).where(
            TeamReplacementQueue.team_id == member.team_id,
            TeamReplacementQueue.lane_key == lane_key,
        ))).scalar_one_or_none()
        if existing:
            return await self._attempt_replacement(existing, db_session, replace_member)
        queue = TeamReplacementQueue(
            team_id=member.team_id, seat_type="premium", lane_key=lane_key,
            next_round=next_round,
        )
        db_session.add(queue)
        await db_session.commit()
        return await self._attempt_replacement(queue, db_session, replace_member)

    async def _attempt_replacement(self, queue, db_session, replace_member):
        queue_id = queue.id
        queue_team_id = queue.team_id
        queue_seat_type = queue.seat_type
        queue_lane = queue.lane_key or f"queue:{queue_id}"
        queue_round = int(queue.next_round or 1)
        team = await db_session.get(Team, queue_team_id)
        if not team or team.rotation_mode != "auto":
            await db_session.delete(queue)
            await db_session.commit()
            return "finished"
        max_rounds = max(0, int(team.rotation_rounds or 0))
        if max_rounds and queue_round > max_rounds:
            await db_session.delete(queue)
            await db_session.commit()
            return "finished"
        try:
            excluded_ids = frozenset(
                int(value) for value in json.loads(queue.excluded_entry_ids or "[]")
                if str(value).isdigit()
            )
        except (TypeError, ValueError):
            excluded_ids = frozenset()
        try:
            result = await replace_member(
                queue_team_id, queue_seat_type, queue_lane,
                queue_round, excluded_ids, db_session,
            )
        except Exception as exc:
            await db_session.rollback()
            result = {"success": False, "error": str(exc)}
        current = await db_session.get(TeamReplacementQueue, queue_id)
        if result.get("success") and result.get("status") == "invited":
            if current:
                await db_session.delete(current)
            await db_session.commit()
            logger.info("自动补位邀请已发送: team=%s lane=%s round=%s email=%s",
                        queue_team_id, queue_lane, queue_round, result.get("email"))
            return "invited"
        if current:
            current.last_attempt_at = get_now()
            current.last_error_code = result.get("error_code") or result.get("status") or "no_candidate"
            candidate_id = result.get("candidate_id")
            if candidate_id and str(candidate_id).isdigit():
                try:
                    attempted = set(json.loads(current.excluded_entry_ids or "[]"))
                except (TypeError, ValueError):
                    attempted = set()
                attempted.add(int(candidate_id))
                current.excluded_entry_ids = json.dumps(sorted(attempted))
            await db_session.commit()
        return "waiting" if result.get("status") == "no_candidate" else "failed"

    async def process_pending_replacements(
        self, db_session: AsyncSession, replace_member: ReplaceMember,
    ) -> Dict[str, int | bool]:
        ids = list((await db_session.execute(
            select(TeamReplacementQueue.id).where(
                TeamReplacementQueue.seat_type == "premium",
            ).order_by(TeamReplacementQueue.id.asc())
        )).scalars().all())
        stats: Dict[str, int | bool] = {
            "success": True, "scanned": len(ids), "invited": 0,
            "waiting": 0, "failed": 0, "finished": 0,
        }
        for queue_id in ids:
            queue = await db_session.get(TeamReplacementQueue, queue_id)
            if not queue:
                continue
            status = await self._attempt_replacement(queue, db_session, replace_member)
            stats[status] = int(stats[status]) + 1
        stats["success"] = stats["failed"] == 0
        return stats

    @staticmethod
    async def _record_failure(member: DueMember, error, db_session: AsyncSession):
        mapping = (await db_session.execute(select(TeamEmailMapping).where(
            TeamEmailMapping.team_id == member.team_id,
            TeamEmailMapping.email == member.email,
        ).execution_options(populate_existing=True))).scalar_one_or_none()
        now = get_now()
        if mapping:
            await db_session.get(Team, member.team_id, populate_existing=True)
            await protect_team_mappings(db_session, member.team_id, [mapping])
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
        warranty = warranty_member_condition(TeamEmailMapping.team_id, TeamEmailMapping.email)
        expires_with_team = or_(warranty, TeamEmailMapping.seat_type == "standard")
        team_expiry_due = expires_with_team & Team.expires_at.is_not(None) & (Team.expires_at <= now)
        timed_due = (~expires_with_team & (TeamEmailMapping.seat_type == "premium")
                     & auto_kick_enabled_condition() & (TeamEmailMapping.auto_kick_at <= now)
                     & ~exists().where(
                         RotationAction.team_id == TeamEmailMapping.team_id,
                         RotationAction.email == TeamEmailMapping.email,
                         RotationAction.status.in_(OPEN_ROTATION_STATUSES)))
        result = await db_session.execute(
            select(TeamEmailMapping)
            .join(Team, Team.id == TeamEmailMapping.team_id)
            .where(
                TeamEmailMapping.status == JOINED_STATUS,
                or_(TeamEmailMapping.auto_kick_retry_at.is_(None), TeamEmailMapping.auto_kick_retry_at <= now,
                    team_expiry_due & or_(TeamEmailMapping.auto_kick_at.is_(None),
                                       TeamEmailMapping.auto_kick_at != Team.expires_at)),
                or_(team_expiry_due, timed_due),
                TeamEmailMapping.upstream_user_id.is_not(None),
                func.coalesce(TeamEmailMapping.member_role, "") != OWNER_ROLE,
                func.lower(func.trim(TeamEmailMapping.email)) != func.lower(func.trim(Team.email)),
            )
            .order_by(TeamEmailMapping.auto_kick_at.asc())
        )
        mappings = result.scalars().all()
        for team_id in {m.team_id for m in mappings}:
            await protect_team_mappings(db_session, team_id, [m for m in mappings if m.team_id == team_id])
        return [
            DueMember(
                mapping.team_id,
                mapping.upstream_user_id,
                mapping.email,
                mapping.id,
                mapping.seat_type,
                getattr(mapping, "rotation_lane", None),
                int(getattr(mapping, "rotation_round", 1) or 1),
            )
            for mapping in mappings
        ]


member_auto_kick_service = MemberAutoKickService()
