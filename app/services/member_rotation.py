"""Manually rotate a Team member with an account-pool replacement."""

from typing import Any

from app.services.member_authorization import MemberAuthorizationError
from app.services.sub2api import Sub2apiError
from app.utils.seats import normalize_seat_type


class MemberRotationService:
    """Coordinate kick, replacement invite, authorization, and Sub2API import."""

    def __init__(self, team_service, account_pool_service, replacement_export_service):
        self.team_service = team_service
        self.account_pool = account_pool_service
        self.exporter = replacement_export_service

    async def rotate(self, team_id: int, user_id: str, email: str, db_session) -> dict[str, Any]:
        member = await self._find_member(team_id, user_id, email, db_session)
        if member is None:
            return {"success": False, "error": "成员不存在或成员列表已变化，请刷新后重试"}
        if member.get("role") == "account-owner":
            return {"success": False, "error": "所有者账号不支持轮转"}

        deleted = await self.team_service.delete_team_member(
            team_id, user_id, db_session, email=member["email"]
        )
        if not deleted.get("success"):
            return {"success": False, "error": deleted.get("error") or "踢出当前成员失败"}

        seat_type = self._replacement_seat_type(member.get("seat_type"))
        replacement = await self.account_pool.invite_replacement(
            team_id,
            db_session,
            invite_member=lambda current_team_id, replacement_email, session, seat_type: (
                self.team_service.add_team_member(
                    current_team_id, replacement_email, session, seat_type=seat_type
                )
            ),
            seat_type=seat_type or "standard",
        )
        if replacement.get("status") != "invited":
            return self._partial_result(
                member["email"], replacement.get("error") or "账号池暂无可用替补账号"
            )

        replacement_email = replacement["email"]
        try:
            export_status = await self.exporter.complete(
                team_id, replacement_email, db_session
            )
        except (MemberAuthorizationError, Sub2apiError) as exc:
            return self._pending_result(member["email"], replacement_email, str(exc))

        return {
            "success": True,
            "completed": export_status == "exported",
            "status": export_status,
            "kicked_email": member["email"],
            "replacement_email": replacement_email,
            "message": self._status_message(export_status, replacement_email),
        }

    async def _find_member(self, team_id, user_id, email, db_session):
        snapshot = await self.team_service.get_team_members(team_id, db_session)
        if not snapshot.get("success"):
            return None
        normalized_email = str(email or "").strip().lower()
        return next(
            (
                member for member in snapshot.get("members", [])
                if member.get("status") == "joined"
                and member.get("user_id") == user_id
                and str(member.get("email") or "").lower() == normalized_email
            ),
            None,
        )

    @staticmethod
    def _replacement_seat_type(value):
        return {"standard": "default", "premium": "premium"}.get(
            normalize_seat_type(value)
        )

    @staticmethod
    def _partial_result(kicked_email, error):
        return {
            "success": True,
            "completed": False,
            "status": "replacement_failed",
            "kicked_email": kicked_email,
            "replacement_email": None,
            "message": f"已踢出 {kicked_email}，但替补流程未完成：{error}",
        }

    @staticmethod
    def _pending_result(kicked_email, replacement_email, error):
        return {
            "success": True,
            "completed": False,
            "status": "export_pending",
            "kicked_email": kicked_email,
            "replacement_email": replacement_email,
            "message": f"已邀请 {replacement_email}，自动授权/导出暂未完成：{error}。后台任务会继续重试。",
        }

    @staticmethod
    def _status_message(status, replacement_email):
        if status == "exported":
            return f"已踢出旧成员、邀请 {replacement_email}，并导入到 sub2api"
        return f"已踢出旧成员、邀请 {replacement_email}，等待新成员接受邀请后自动导出到 sub2api"
