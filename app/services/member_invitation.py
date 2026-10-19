"""Invite members using the live balance of the requested seat type."""
import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.models import Team, TeamEmailMapping
from app.utils.seats import normalize_seat_type
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = ("joined", "invited")
CLIENT_ERROR_MIN = 400
CLIENT_ERROR_MAX = 500


@dataclass(frozen=True, kw_only=True)
class InviteRequest:
    team_id: int
    email: str
    seat_type: str | None = None
    allow_reinvite: bool = False


class MemberInvitationService:
    def __init__(self, teams, db):
        self.teams = teams
        self.db = db

    async def invite(self, request):
        seat = {"standard": "default", "premium": "premium"}.get(
            normalize_seat_type(request.seat_type or "default")
        )
        if seat is None:
            return self.teams._admin_error("invalid_seat_type", "不支持的席位类型")
        email = self.teams._normalize_member_email(request.email)
        if not email or not self.teams.token_parser.validate_email_format(email):
            return self.teams._admin_error("invalid_email", "邮箱格式不正确")
        target = InviteRequest(team_id=request.team_id, email=email, seat_type=seat,
                               allow_reinvite=request.allow_reinvite)
        try:
            return await self._invite(target)
        except Exception:
            await self.db.rollback()
            logger.exception("添加成员失败: team=%s email=%s", target.team_id, email)
            return self.teams._admin_error("invite_failed", "添加成员失败，请检查服务端日志")

    async def _invite(self, target):
        team = await self.db.get(Team, target.team_id)
        if team is None:
            return self.teams._admin_error("team_not_found", "Team 不存在")
        if team.status == "expired":
            return self.teams._admin_error("team_expired", "Team 已过期,无法添加成员")
        token = await self.teams.ensure_access_token(team, self.db)
        if not token:
            return self.teams._admin_error(
                "token_refresh_failed", "该 Team 的登录凭证已过期，且自动刷新失败，请重新登录或重新导入"
            )
        sync = await self.teams.sync_team_info(target.team_id, self.db)
        if not sync.get("success"):
            return self.teams._admin_error(
                "team_sync_failed", sync.get("error") or "拉取 Team 最新成员状态失败，请稍后重试"
            )
        emails = {self.teams._normalize_member_email(email) for email in sync.get("member_emails", [])}
        if target.email in emails:
            return {"success": True, "message": f"{target.email} 已在该 Team 中", "error": None,
                    "status": "already_exists", "email": target.email}
        error = await self._check_membership(target)
        if error:
            return error
        balance = await self.teams.get_team_seat_balance(team, self.db, token)
        error = self.teams._seat_balance_error(balance, target.seat_type)
        if error:
            return error
        error = await self.teams._reserve_member_seat(
            team, "invite", target.email, target.seat_type, self.db
        )
        if error:
            return error
        result = await self.teams.chatgpt_service.send_invite(
            token, team.account_id, target.email, self.db,
            identifier=team.email, seat_type=target.seat_type,
        )
        if not result["success"]:
            return await self._rejected(team, result, target=target)
        return await self._accepted(team, result, target=target)

    async def _check_membership(self, target):
        result = await self.db.execute(select(TeamEmailMapping.team_id).where(
            TeamEmailMapping.email == target.email,
            TeamEmailMapping.status.in_(ACTIVE_STATUSES),
        ))
        foreign = list(dict.fromkeys(team_id for team_id in result.scalars().all()
                                     if team_id != target.team_id))
        if foreign:
            return self.teams._admin_error(
                "email_already_in_team", "该邮箱已加入或已邀请到其他 Team，默认一个账号只能加入一个 Team",
                team_ids=foreign,
            )
        if target.allow_reinvite:
            return None
        retry_at = await self.teams.get_reinvite_retry_at(target.team_id, target.email, self.db)
        if retry_at:
            return self.teams._admin_error(
                "team_reinvite_cooldown", f"该账号 7 天内已被当前 Team 邀请过，可在 {retry_at.isoformat()} 后重试",
                retry_at=retry_at.isoformat(),
            )
        return None

    async def _rejected(self, team, result, *, target):
        if CLIENT_ERROR_MIN <= result.get("status_code", 0) < CLIENT_ERROR_MAX:
            await self.teams._release_member_seat(team.account_id, "invite", target.email, self.db)
        await self.teams._handle_api_error(result, team, self.db)
        messages = {"account_deactivated": "账号已封禁 (account_deactivated)",
                    "token_invalidated": "Token 已失效 (token_invalidated)"}
        code = result.get("error_code") or "invite_failed"
        return self.teams._admin_error(code, messages.get(code) or f"发送邀请失败: {result['error']}")

    async def _accepted(self, team, result, *, target):
        data = result.get("data", {})
        if "account_invites" in data and not data["account_invites"]:
            await self.teams._release_member_seat(team.account_id, "invite", target.email, self.db)
            error = self.teams._admin_error("ghost_success", "官方拦截下发(响应空列表)")
            await self.teams._handle_api_error(error, team, self.db)
            return self.teams._admin_error(
                "ghost_success", "Team账号受限: 官方拦截下发(响应空列表)，请检查账单/风控状态"
            )
        await self._record(team, target)
        await self.teams._reset_error_status(team, self.db)
        self._schedule_verification(target)
        logger.info("添加成员成功: %s -> Team %s", target.email, target.team_id)
        return {"success": True, "message": f"邀请已发送到 {target.email}", "error": None,
                "status": "invited", "email": target.email}

    async def _record(self, team, target):
        result = await self.db.execute(select(TeamEmailMapping.status).where(
            TeamEmailMapping.team_id == target.team_id, TeamEmailMapping.email == target.email,
        ))
        had_active_mapping = result.scalar_one_or_none() in ACTIVE_STATUSES
        await self.teams.upsert_team_email_mapping(
            team_id=target.team_id, email=target.email, status="invited", db_session=self.db,
            source="admin_add", is_admin_invited=True, invited_at=get_now(),
        )
        if not had_active_mapping:
            team.current_members += 1
        if team.expires_at and team.expires_at < get_now():
            team.status = "expired"
        elif team.current_members >= team.max_members:
            team.status = "full"
        else:
            team.status = "active"
        await self.db.commit()

    def _schedule_verification(self, target):
        task = asyncio.create_task(self.teams._background_verify_admin_invite(target.team_id, target.email))
        self.teams._background_tasks.add(task)
        task.add_done_callback(self.teams._background_tasks.discard)
