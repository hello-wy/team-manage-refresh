"""Verify owners with their own Team token when no password is stored."""
import logging

from sqlalchemy import update

from app.models import Team
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


class AccountPoolOwnerLivenessService:
    def __init__(self, teams):
        self.teams = teams

    async def check(self, session, team_id):
        team = await session.get(Team, team_id)
        if team is None:
            return None
        version = (team.email, team.account_id, team.access_token_encrypted)
        status, message = "error", "所有者授权检测异常，请重试"
        try:
            token = await self.teams.ensure_access_token(team, session)
            # Token refresh may commit; guard the result against subsequent edits.
            version = (team.email, team.account_id, team.access_token_encrypted)
            if not token:
                message = "所有者 Token 不可用或刷新失败，请在工作台更新授权"
            elif str(self.teams.jwt_parser.extract_email(token) or "").strip().lower() != team.email.strip().lower():
                status, message = "invalid", "Token 邮箱与所有者不一致，请在工作台更新授权"
            else:
                result = await self.teams.chatgpt_service.get_account_info(
                    token, session, identifier=team.email,
                )
                if result.get("success"):
                    account = next((a for a in result.get("accounts", [])
                                    if a.get("account_id") == team.account_id), None)
                    if account and account.get("account_user_role") == "account-owner":
                        status = "alive"
                        message = "所有者 Team Token 验活通过（未验证密码与 2FA）"
                    else:
                        message = "Token 未确认当前 Team 的所有者身份，请在工作台检查授权"
                else:
                    invalid = result.get("error_code") in {
                        "account_deactivated", "token_invalidated", "token_expired", "invalid_token",
                    } or result.get("status_code") == 401
                    status = "invalid" if invalid else "error"
                    message = ("所有者 Token 已失效，请在工作台更新授权" if invalid else
                               "所有者授权检测请求失败，请稍后重试")
        except Exception:
            logger.exception("所有者验活异常: team_id=%s", team_id)
            await session.rollback()
        saved = await session.execute(update(Team).where(
            Team.id == team_id, Team.email == version[0], Team.account_id == version[1],
            Team.access_token_encrypted == version[2],
        ).values(owner_liveness_status=status, owner_liveness_message=message,
                 owner_liveness_checked_at=get_now()))
        await session.commit()
        return status if saved.rowcount else None
