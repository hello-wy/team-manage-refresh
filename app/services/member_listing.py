"""Build member snapshots and persist only the membership evidence read so far."""
from dataclasses import dataclass

from sqlalchemy import select

from app.models import MemberAuthorization, Team
from app.services.member_auto_kick import DEFAULT_MEMBER_AUTO_KICK_HOURS
from app.services.member_auto_kick_policy import is_auto_kick_exempt
from app.services.member_sync_cursor import fetch_sync_members
from app.utils.seats import summarize_member_seats
from app.utils.time_utils import get_now


@dataclass(frozen=True)
class ListingContext:
    team: object
    token: str
    db: object
    members: dict
    invites: dict


def joined_member(member):
    return {
        "user_id": member.get("id"), "email": member.get("email"),
        "name": member.get("name"), "role": member.get("role"),
        "seat_type": member.get("seat_type"), "pending_seat_type": member.get("pending_seat_type"),
        "added_at": member.get("created_time"), "status": "joined",
        "profile_sync_error": member.get("profile_sync_error"),
        "profile_cached_fields": member.get("profile_cached_fields"),
    }


def invited_member(invite):
    return {
        "user_id": None, "email": invite.get("email_address"), "name": None,
        "role": invite.get("role"), "seat_type": invite.get("seat_type"),
        "added_at": invite.get("created_time"), "status": "invited",
    }


class MemberListingService:
    def __init__(self, team_service, *, cipher, sync_owner):
        self.teams = team_service
        self.cipher = cipher
        self.sync_owner = sync_owner

    async def load(self, team_id, db, *, pagination=None):
        team = await db.get(Team, team_id)
        if team is None:
            raise ValueError(f"Team ID {team_id} 不存在")
        token = await self.teams.ensure_access_token(team, db)
        if not token:
            raise ValueError("Token 已过期且无法刷新")
        remote = self.teams.chatgpt_service
        if pagination is None:
            members = await remote.get_members(token, team.account_id, db)
        else:
            members = await fetch_sync_members(
                remote, (team, token, db), cursor=pagination.get("cursor"), cipher=self.cipher,
            )
        await self._check_result(members, team, db=db)
        members = await self.teams._normalize_member_profiles(team, members, db)
        invites = await remote.get_invites(token, team.account_id, db)
        if not invites["success"] and await self.teams._handle_api_error(invites, team, db):
            raise ValueError(invites.get("error") or "获取邀请列表失败")
        context = ListingContext(team, token, db, members, invites)
        return await self._snapshot(context)

    async def _check_result(self, result, team, *, db):
        if result["success"]:
            return
        await self.teams._handle_api_error(result, team, db)
        labels = {"account_deactivated": "账号已封禁 (account_deactivated)",
                  "token_invalidated": "Token 已失效 (token_invalidated)"}
        raise ValueError(labels.get(result.get("error_code"))
                         or f"获取成员列表失败: {result.get('error', '未知错误')}")

    def _merge(self, context):
        members = context.members["members"]
        emails = {member.get("email") for member in members if member.get("email")}
        invites = self.teams._filter_pending_invites(
            context.invites.get("items", []) if context.invites["success"] else [],
            joined_emails=emails,
        )
        return [*map(joined_member, members), *map(invited_member, invites)]

    async def _persist(self, context, all_members):
        team, db, members = context.team, context.db, context.members
        pagination = members.get("pagination", {})
        complete = not pagination.get("has_more", False)
        joined = {m["email"]: m for m in members["members"] if m.get("email")}
        invited = {m["email"] for m in all_members if m["status"] == "invited" and m.get("email")}
        await self.teams._reconcile_team_email_mappings(
            team.id, set(joined), invited, db, joined_members=joined,
            member_identities_complete=(complete and context.invites["success"]
                                        and all(m.get("email") for m in members["members"])),
        )
        team.joined_members = members["total"]
        if complete:
            team.last_sync = get_now()
        effective = await self.teams._apply_member_count_floor(team, members["total"] + len(invited), db)
        await self.teams._reset_error_status(team, db)
        return effective

    async def _balance(self, context):
        team, token, db = context.team, context.token, context.db
        if context.members.get("pagination", {}).get("has_more"):
            await self.teams._refresh_member_display(team, context.members, token, db)
            await db.commit()
            return {"success": False, "error": "成员同步中，完整读取后更新席位余额"}
        return await self.teams.get_team_seat_balance(team, db, token, context.members, context.invites)

    async def _decorate(self, context, members):
        team, db = context.team, context.db
        await self.sync_owner(team, context.token, context.members["members"], db)
        records = (await db.execute(select(MemberAuthorization).where(
            MemberAuthorization.team_id == team.id,
            MemberAuthorization.account_id == team.account_id,
            MemberAuthorization.credentials_encrypted.is_not(None),
        ))).scalars().all()
        authorizations = {record.email: record for record in records}
        mappings = await self.teams._team_mapping_index(team.id, db)
        return [self._decorate_member(member, authorizations, mappings=mappings) for member in members]

    @staticmethod
    def _decorate_member(member, authorizations, *, mappings):
        authorization, mapping = authorizations.get(member["email"]), mappings.get(member["email"])
        return {
            **member, "authorized": authorization is not None,
            "json_saved": bool(authorization and authorization.export_json_encrypted),
            "sub2api_exported": bool(authorization and authorization.sub2api_exported_at),
            "auto_kick_at": (mapping.auto_kick_at.isoformat()
                             if mapping and mapping.auto_kick_at and not is_auto_kick_exempt(mapping) else None),
            "auto_kick_exempt": bool(mapping and is_auto_kick_exempt(mapping)),
        }

    async def _snapshot(self, context):
        all_members = self._merge(context)
        effective = await self._persist(context, all_members)
        balance = await self._balance(context)
        decorated = await self._decorate(context, all_members)
        team = context.team
        result = {
            "success": True, "members": decorated,
            "seat_summary": summarize_member_seats(decorated, context.invites["success"]),
            "seat_balance": balance, "joined_members": team.joined_members,
            "total_seats": team.total_seats, "team_status": team.effective_status,
            "total": effective,
            "member_auto_kick_hours": int(team.member_auto_kick_hours or DEFAULT_MEMBER_AUTO_KICK_HOURS),
            "error": None,
        }
        if "pagination" in context.members:
            return {**result, "pagination": context.members["pagination"]}
        return result
