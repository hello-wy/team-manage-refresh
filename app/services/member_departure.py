"""Fresh account-pool login for scheduled member departure."""
import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.models import AccountPoolEntry, Team, TeamEmailMapping
from app.services.account_pool_authorization import AccountPoolAuthorizationError
from app.services.member_auto_kick_policy import participates
from app.services.member_identity import credential_identity
from app.utils.time_utils import get_now
from app.services.warranty_member_protection import protect_team_mappings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemberExitTarget:
    team: object
    user_id: str
    email: str
    db: object


class AccountPoolMemberExit:
    def __init__(self, *, authorization, remote):
        self.authorization = authorization
        self.remote = remote

    async def __call__(self, target):
        mapping = await self._mapping(target)
        if not self._still_due(mapping, target.user_id):
            return {"success": False, "error": "成员的下线计划或身份已变化，取消本次下线"}
        try:
            credentials = await self._login(target)
        except AccountPoolAuthorizationError as exc:
            if not self._still_due(await self._mapping(target), target.user_id):
                return {"success": False, "error": "登录期间下线计划已变化，取消本次下线"}
            return self._admin_required(target, str(exc))
        if not self._still_due(await self._mapping(target), target.user_id):
            return {"success": False, "error": "登录期间下线计划已变化，取消本次下线"}
        result = await self.remote.delete_workspace_user(
            credentials["access_token"], target.team.account_id, target.user_id,
            target.db, identifier=f"member-departure-{mapping.id}",
        )
        if not result.get("success"):
            return {**result, **self._admin_required(target, result.get("error") or "成员自退失败")}
        logger.info("到期成员登录自退请求成功: team=%s email=%s", target.team.id, target.email)
        return result

    @staticmethod
    async def _mapping(target):
        mapping = (await target.db.execute(select(TeamEmailMapping).where(
            TeamEmailMapping.team_id == target.team.id,
            TeamEmailMapping.email == target.email,
        ).execution_options(populate_existing=True))).scalar_one_or_none()
        if mapping:
            await target.db.get(Team, target.team.id, populate_existing=True)
            await protect_team_mappings(target.db, target.team.id, [mapping])
        return mapping

    @staticmethod
    def _still_due(mapping, user_id):
        return bool(mapping and mapping.status == "joined"
                    and mapping.upstream_user_id == user_id
                    and mapping.member_role != "account-owner"
                    and participates(mapping)
                    and mapping.auto_kick_at
                    and mapping.auto_kick_at <= get_now())

    async def _login(self, target):
        entry = await self._entry(target)
        version = (entry.password_encrypted, entry.two_factor_secret_encrypted)
        result = await self.authorization.login_entry(target.db, entry.id, target.team.account_id)
        identity = credential_identity(result.payload, target.email, target.team.account_id)
        if identity != (target.user_id, target.email):
            raise AccountPoolAuthorizationError("登录身份与到期成员不一致，不能使用此凭据自退")
        saved = await self.authorization.save_result(
            target.db, entry.id, result, credential_version=version, update_current=False,
        )
        if not saved:
            raise AccountPoolAuthorizationError("账号登录凭据已变化，无法保存本次自退授权")
        return result.payload["accounts"][0]["credentials"]

    @staticmethod
    async def _entry(target):
        entry = (await target.db.execute(select(AccountPoolEntry).where(
            AccountPoolEntry.email == target.email,
            AccountPoolEntry.deleted_at.is_(None),
        ))).scalar_one_or_none()
        if entry is None:
            raise AccountPoolAuthorizationError("账号号池未保存该成员，无法自动登录自退")
        return entry

    async def confirm_departure(self, target):
        """管理员失效时，通过成员重新登录后的空间列表确认真实退组。"""
        try:
            entry = await self._entry(target)
            scan = await self.authorization.scan_entry(target.db, entry.id)
        except AccountPoolAuthorizationError as exc:
            logger.warning("成员自退复核失败: team=%s email=%s error=%s",
                           target.team.id, target.email, exc)
            return {"success": False, "error": f"成员自退已返回成功，但重新登录复核失败：{exc}"}
        options = scan.get("available_workspaces")
        if not isinstance(options, list) or not options or any(
            not isinstance(item, dict) or not item.get("id") for item in options
        ):
            return {"success": False, "error": "成员自退已返回成功，但未取得完整空间列表"}
        if any(item["id"] == target.team.account_id for item in options):
            return {"success": False, "error": "成员自退已返回成功，但重新登录后仍可见原 Team"}
        logger.info("成员重新登录已确认离组: team=%s email=%s", target.team.id, target.email)
        return {"success": True, "message": "成员已自行退出，重新登录确认原 Team 已移除",
                "removal_method": "self", "admin_unavailable": True, "error": None}

    @staticmethod
    def _admin_required(target, error):
        logger.warning("到期成员无法登录自退，改由管理员踢出: team=%s email=%s error=%s",
                       target.team.id, target.email, error)
        return {"success": False, "error": error, "admin_removal_required": True}
