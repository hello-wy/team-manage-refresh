"""后台账号号池及成员加入历史服务。"""
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolHistory, Team, TeamEmailMapping
from app.services.account_pool_credentials import (
    AccountPoolCredentialService,
    account_pool_credential_service,
)
from app.services.account_pool_history import account_pool_history_service
from app.services.account_pool_listing import ACTIVE_MAPPING_STATUSES, build_pool_entry_data
from app.services.account_pool_replacement import mark_replacement_pending
from app.utils.time_utils import get_now

TEAM_REINVITE_COOLDOWN_DAYS = 7
TEAM_REJOIN_COOLDOWN_DAYS = 7

InviteMember = Callable[..., Awaitable[dict[str, Any]]]


class AccountPoolService:
    """维护邮箱号池，并把 Team 同步结果转换为可追溯历史。"""

    def __init__(self, credential_service: AccountPoolCredentialService):
        self._credential_service = credential_service

    @staticmethod
    def normalize_email(value: Any) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def parse_emails(
        emails: Optional[list[str]] = None,
        content: str = "",
    ) -> tuple[list[str], list[str]]:
        records, invalid = AccountPoolCredentialService.parse_account_inputs(
            emails,
            content,
        )
        return [record.email for record in records], invalid

    async def add_emails(
        self,
        db_session: AsyncSession,
        *,
        emails: Optional[list[str]] = None,
        content: str = "",
    ) -> dict[str, Any]:
        return await self._credential_service.add_accounts(
            db_session,
            emails=emails,
            content=content,
        )

    async def find_replacement_candidate(
        self,
        team_id: int,
        db_session: AsyncSession,
    ) -> Optional[AccountPoolEntry]:
        cutoff = get_now() - timedelta(days=TEAM_REINVITE_COOLDOWN_DAYS)
        active_mapping = select(TeamEmailMapping.id).where(
            TeamEmailMapping.email == AccountPoolEntry.email,
            TeamEmailMapping.status.in_(ACTIVE_MAPPING_STATUSES),
        )
        recent_team_invite = select(TeamEmailMapping.id).where(
            TeamEmailMapping.team_id == team_id,
            TeamEmailMapping.email == AccountPoolEntry.email,
            TeamEmailMapping.last_invited_at.is_not(None),
            TeamEmailMapping.last_invited_at > cutoff,
        )
        result = await db_session.execute(
            select(AccountPoolEntry)
            .where(
                AccountPoolEntry.deleted_at.is_(None),
                ~active_mapping.exists(),
                ~recent_team_invite.exists(),
            )
            .order_by(AccountPoolEntry.updated_at.asc(), AccountPoolEntry.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_invite_options(
        self,
        team_id: int,
        db_session: AsyncSession,
    ) -> Optional[list[dict[str, Any]]]:
        team = await db_session.get(Team, team_id)
        if not team:
            return None

        cutoff = get_now() - timedelta(days=TEAM_REJOIN_COOLDOWN_DAYS)
        active_mapping = select(TeamEmailMapping.id).where(
            TeamEmailMapping.email == AccountPoolEntry.email,
            TeamEmailMapping.status.in_(ACTIVE_MAPPING_STATUSES),
        )
        conditions = [
            AccountPoolEntry.deleted_at.is_(None),
            ~active_mapping.exists(),
            or_(
                AccountPoolEntry.workspace_id.is_(None),
                func.trim(AccountPoolEntry.workspace_id) == "",
                AccountPoolEntry.workspace_status == "personal_account",
                AccountPoolEntry.workspace_id == team.account_id,
            ),
        ]
        owner_email = self.normalize_email(team.email)
        if owner_email:
            conditions.append(AccountPoolEntry.email != owner_email)

        result = await db_session.execute(
            select(AccountPoolEntry)
            .where(*conditions)
            .order_by(AccountPoolEntry.email.asc())
        )
        entries = result.scalars().all()
        if not entries:
            return []

        recent_result = await db_session.execute(
            select(
                AccountPoolHistory.account_pool_id,
                func.max(AccountPoolHistory.joined_at),
            )
            .where(
                AccountPoolHistory.account_pool_id.in_([entry.id for entry in entries]),
                AccountPoolHistory.team_id == team_id,
                AccountPoolHistory.joined_at > cutoff,
            )
            .group_by(AccountPoolHistory.account_pool_id)
        )
        recent_entry_ids = {account_pool_id for account_pool_id, _ in recent_result.all()}
        return [
            {
                "id": entry.id,
                "email": entry.email,
                "recently_joined": entry.id in recent_entry_ids,
            }
            for entry in entries
        ]

    async def invite_replacement(
        self,
        team_id: int,
        db_session: AsyncSession,
        *,
        invite_member: InviteMember,
        seat_type: str = "standard",
    ) -> dict[str, Any]:
        candidate = await self.find_replacement_candidate(team_id, db_session)
        if not candidate:
            return {"success": True, "status": "no_candidate", "email": None}

        result = await invite_member(
            team_id,
            candidate.email,
            db_session,
            seat_type=seat_type,
        )
        if not result.get("success") or result.get("status") != "invited":
            return {
                "success": False,
                "status": "failed",
                "email": candidate.email,
                "error": result.get("error") or result.get("message") or "邀请失败",
                "error_code": result.get("error_code"),
                "status_code": result.get("status_code"),
            }
        await mark_replacement_pending(db_session, team_id, candidate.email)
        return {"success": True, "status": "invited", "email": candidate.email}

    async def record_reconciliation(
        self,
        db_session: AsyncSession,
        *,
        team: Optional[Team],
        mappings: list[TeamEmailMapping],
        previous_states: dict[str, tuple[Optional[str], Optional[datetime]]],
        seen_at: datetime,
    ) -> None:
        await account_pool_history_service.record_reconciliation(
            db_session,
            team=team,
            mappings=mappings,
            previous_states=previous_states,
            seen_at=seen_at,
        )
    async def list_entries(
        self,
        db_session: AsyncSession,
        page: int = 1,
        per_page: int = 20,
        search: str = "",
        status_filter: str = "",
    ) -> dict[str, Any]:
        page = max(page, 1)
        per_page = min(max(per_page, 1), 100)
        conditions = [AccountPoolEntry.deleted_at.is_(None)]
        normalized_search = self.normalize_email(search)
        if normalized_search:
            conditions.append(AccountPoolEntry.email.ilike(f"%{normalized_search}%"))

        base_query = select(AccountPoolEntry).where(*conditions).order_by(
            AccountPoolEntry.created_at.desc(), AccountPoolEntry.id.desc()
        )
        if status_filter:
            all_entries = list((await db_session.execute(base_query)).scalars().all())
            data = [
                item for item in await self._build_entry_data(db_session, all_entries)
                if item["status"] == status_filter
            ]
            total = len(data)
            start = (page - 1) * per_page
            data = data[start:start + per_page]
        else:
            total_result = await db_session.execute(
                select(func.count(AccountPoolEntry.id)).where(*conditions)
            )
            total = int(total_result.scalar() or 0)
            entries_result = await db_session.execute(
                base_query.offset((page - 1) * per_page).limit(per_page)
            )
            data = await self._build_entry_data(db_session, list(entries_result.scalars().all()))
        total_pages = max((total + per_page - 1) // per_page, 1)
        return {
            "entries": data,
            "total": total,
            "total_pages": total_pages,
            "current_page": page,
            "per_page": per_page,
        }
    async def _build_entry_data(
        self,
        db_session: AsyncSession,
        entries: list[AccountPoolEntry],
    ) -> list[dict[str, Any]]:
        return await build_pool_entry_data(db_session, entries)
    async def get_history(self, db_session: AsyncSession, entry_id: int) -> Optional[dict[str, Any]]:
        return await account_pool_history_service.get_history(db_session, entry_id)

    async def get_login_targets(
        self,
        db_session: AsyncSession,
        entry_id: int,
    ) -> Optional[dict[str, Any]]:
        entry = await db_session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            return None
        result = await db_session.execute(
            select(TeamEmailMapping, Team)
            .join(Team, Team.id == TeamEmailMapping.team_id)
            .where(
                TeamEmailMapping.email == entry.email,
                TeamEmailMapping.status.in_(ACTIVE_MAPPING_STATUSES),
            )
            .order_by(Team.id.asc())
        )
        targets = []
        seen_team_ids = set()
        for mapping, team in result.all():
            if team.id in seen_team_ids:
                continue
            seen_team_ids.add(team.id)
            targets.append({
                "id": team.id,
                "name": team.team_name,
                "email": team.email,
                "status": mapping.status,
            })
        return {"entry_id": entry.id, "email": entry.email, "teams": targets}


account_pool_service = AccountPoolService(account_pool_credential_service)
