"""后台账号号池及成员加入历史服务。"""
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolHistory, Team, TeamEmailMapping
from app.services.account_pool_credentials import (
    AccountPoolCredentialService,
    account_pool_credential_service,
)
from app.services.account_pool_history import account_pool_history_service
from app.services.account_pool_listing import ACTIVE_MAPPING_STATUSES, build_pool_entry_data
from app.services.account_pool_owners import owner_email_exists
from app.services.account_pool_replacement import mark_replacement_pending
from app.utils.time_utils import get_now
from app.utils.team_names import team_display_name

TEAM_REINVITE_COOLDOWN_DAYS = 7
TEAM_REJOIN_COOLDOWN_DAYS = 7


def _matches_status_filter(row: dict[str, Any], status_filter: str) -> bool:
    if status_filter == "joined":
        return not row["workspace_is_personal"]
    if status_filter == "not_joined":
        return row["workspace_is_personal"]
    return row["status"] == status_filter

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
        preserve_existing_credentials: bool = False,
    ) -> dict[str, Any]:
        return await self._credential_service.add_accounts(
            db_session,
            emails=emails,
            content=content,
            preserve_existing_credentials=preserve_existing_credentials,
        )

    async def find_replacement_candidate(
        self,
        team_id: int,
        db_session: AsyncSession,
        *,
        excluded_ids: frozenset[int] = frozenset(),
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
                ~owner_email_exists(),
                ~recent_team_invite.exists(),
                ~AccountPoolEntry.id.in_(excluded_ids),
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
            ~owner_email_exists(),
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
        # Union identities before pagination so owners participate in search,
        # counts and filters, without inserting them into the replacement pool.
        pool = select(AccountPoolEntry.id, AccountPoolEntry.email, AccountPoolEntry.created_at).where(
            AccountPoolEntry.deleted_at.is_(None))
        owner_email = func.lower(func.trim(Team.email))
        in_pool = select(AccountPoolEntry.id).where(
            AccountPoolEntry.deleted_at.is_(None),
            func.lower(func.trim(AccountPoolEntry.email)) == owner_email,
        ).exists()
        owners = select((-func.min(Team.id)).label("id"), owner_email.label("email"),
                        func.min(Team.created_at).label("created_at")).where(
            ~in_pool, owner_email != "",
        ).group_by(owner_email)
        identities = union_all(pool, owners).subquery()
        query = select(identities.c.id).order_by(identities.c.created_at.desc(), identities.c.id.desc())
        normalized_search = self.normalize_email(search)
        if normalized_search:
            query = query.where(identities.c.email.ilike(f"%{normalized_search}%"))
        if status_filter:
            ids = list((await db_session.execute(query)).scalars())
            data = [row for row in await self.rows_by_ids(db_session, ids)
                    if _matches_status_filter(row, status_filter)]
            total = len(data)
            data = data[(page - 1) * per_page:page * per_page]
        else:
            total = (await db_session.execute(select(func.count()).select_from(
                query.order_by(None).subquery()))).scalar_one()
            ids = list((await db_session.execute(query.offset((page - 1) * per_page).limit(per_page))).scalars())
            data = await self.rows_by_ids(db_session, ids)
        total_pages = max((total + per_page - 1) // per_page, 1)
        return {
            "entries": data,
            "total": total,
            "total_pages": total_pages,
            "current_page": page,
            "per_page": per_page,
        }
    async def rows_by_ids(self, db_session, ids):
        if not ids:
            return []
        entries = list((await db_session.execute(select(AccountPoolEntry).where(
            AccountPoolEntry.id.in_([item for item in ids if item > 0]),
            AccountPoolEntry.deleted_at.is_(None),
        ))).scalars())
        if any(item < 0 for item in ids):
            teams = (await db_session.execute(select(Team).where(
                Team.id.in_([-item for item in ids if item < 0])
            ))).scalars()
            entries.extend(AccountPoolEntry(
                id=-team.id, email=team.email.strip().lower(), created_at=team.created_at,
                workspace_id=team.account_id, workspace_name=team.team_name,
                workspace_status="team", liveness_status=None,
            ) for team in teams)
        rows = {row["id"]: row for row in await self._build_entry_data(db_session, entries)}
        return [rows[item] for item in ids if item in rows]

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
                "name": team_display_name(team),
                "email": team.email,
                "status": mapping.status,
            })
        return {"entry_id": entry.id, "email": entry.email, "teams": targets}


account_pool_service = AccountPoolService(account_pool_credential_service)
