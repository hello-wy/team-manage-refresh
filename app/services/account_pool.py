"""后台账号号池及成员加入历史服务。"""
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolHistory, Team, TeamEmailMapping
from app.services.account_pool_history import account_pool_history_service
from app.utils.time_utils import get_now

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACTIVE_MAPPING_STATUSES = ("invited", "joined")
TEAM_REINVITE_COOLDOWN_DAYS = 7
TEAM_REJOIN_COOLDOWN_DAYS = 7

InviteMember = Callable[..., Awaitable[dict[str, Any]]]


class AccountPoolService:
    """维护邮箱号池，并把 Team 同步结果转换为可追溯历史。"""

    @staticmethod
    def normalize_email(value: Any) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def parse_emails(cls, emails: Optional[list[str]] = None, content: str = "") -> tuple[list[str], list[str]]:
        values = list(emails or [])
        if content:
            values.extend(content.splitlines())

        normalized: list[str] = []
        invalid: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            email = cls.normalize_email(raw_value)
            if not email:
                continue
            if not EMAIL_PATTERN.fullmatch(email):
                invalid.append(str(raw_value).strip())
                continue
            if email not in seen:
                seen.add(email)
                normalized.append(email)
        return normalized, invalid
    async def add_emails(
        self,
        db_session: AsyncSession,
        *,
        emails: Optional[list[str]] = None,
        content: str = "",
    ) -> dict[str, Any]:
        normalized, invalid = self.parse_emails(emails, content)
        if not normalized:
            return {
                "success": False,
                "message": "未发现可添加的有效邮箱",
                "added": [],
                "existing": [],
                "invalid": invalid,
            }

        result = await db_session.execute(
            select(AccountPoolEntry).where(AccountPoolEntry.email.in_(normalized))
        )
        existing = {entry.email: entry for entry in result.scalars().all()}
        added = []
        for email in normalized:
            entry = existing.get(email)
            if entry:
                continue
            entry = AccountPoolEntry(email=email)
            db_session.add(entry)
            existing[email] = entry
            added.append(email)

        await db_session.flush()
        await account_pool_history_service.backfill_current_histories(
            db_session, list(existing.values())
        )
        await db_session.commit()
        return {
            "success": bool(normalized),
            "message": self._build_add_message(normalized, added),
            "added": added,
            "updated": [],
            "existing": [email for email in normalized if email not in added],
            "invalid": invalid,
        }

    @staticmethod
    def _build_add_message(normalized, added) -> str:
        return f"新增 {len(added)} 个邮箱，已存在 {len(normalized) - len(added)} 个"

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
            .where(~active_mapping.exists(), ~recent_team_invite.exists())
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
        recent_team_join = select(AccountPoolHistory.id).where(
            AccountPoolHistory.account_pool_id == AccountPoolEntry.id,
            AccountPoolHistory.team_id == team_id,
            AccountPoolHistory.joined_at > cutoff,
        )
        conditions = [~active_mapping.exists(), ~recent_team_join.exists()]
        owner_email = self.normalize_email(team.email)
        if owner_email:
            conditions.append(AccountPoolEntry.email != owner_email)

        result = await db_session.execute(
            select(AccountPoolEntry)
            .where(*conditions)
            .order_by(AccountPoolEntry.email.asc())
        )
        return [
            {"id": entry.id, "email": entry.email}
            for entry in result.scalars().all()
        ]

    async def invite_replacement(
        self,
        team_id: int,
        db_session: AsyncSession,
        *,
        invite_member: InviteMember,
    ) -> dict[str, Any]:
        candidate = await self.find_replacement_candidate(team_id, db_session)
        if not candidate:
            return {"success": True, "status": "no_candidate", "email": None}

        result = await invite_member(
            team_id,
            candidate.email,
            db_session,
        )
        if not result.get("success") or result.get("status") != "invited":
            return {
                "success": False,
                "status": "failed",
                "email": candidate.email,
                "error": result.get("error") or result.get("message") or "邀请失败",
            }
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
        conditions = []
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
        if not entries:
            return []
        entry_ids = [entry.id for entry in entries]
        mapping_result = await db_session.execute(
            select(TeamEmailMapping, Team)
            .join(Team, Team.id == TeamEmailMapping.team_id)
            .where(
                TeamEmailMapping.email.in_([entry.email for entry in entries]),
                TeamEmailMapping.status.in_(ACTIVE_MAPPING_STATUSES),
            )
        )
        mapping_by_email: dict[str, list[tuple[TeamEmailMapping, Team]]] = defaultdict(list)
        for mapping, team in mapping_result.all():
            mapping_by_email[mapping.email].append((mapping, team))

        histories_result = await db_session.execute(
            select(AccountPoolHistory)
            .where(AccountPoolHistory.account_pool_id.in_(entry_ids))
            .order_by(AccountPoolHistory.joined_at.desc())
        )
        histories_by_entry: dict[int, list[AccountPoolHistory]] = defaultdict(list)
        for history in histories_result.scalars().all():
            histories_by_entry[history.account_pool_id].append(history)

        items = []
        for entry in entries:
            mappings = mapping_by_email.get(entry.email, [])
            joined = [item for item in mappings if item[0].status == "joined"]
            invited = [item for item in mappings if item[0].status == "invited"]
            active = joined + invited
            status = "unassigned"
            if len({team.id for _, team in active}) > 1:
                status = "conflict"
            elif joined:
                status = "joined"
            elif invited:
                status = "invited"
            current = active[0] if active else (None, None)
            seat_type = next(
                (mapping.seat_type for mapping, _ in joined if mapping.seat_type),
                None,
            )
            histories = histories_by_entry.get(entry.id, [])
            items.append({
                "id": entry.id,
                "email": entry.email,
                "seat_type": seat_type,
                "status": status,
                "team_id": current[1].id if current[1] else None,
                "team_name": current[1].team_name if current[1] else None,
                "team_email": current[1].email if current[1] else None,
                "joined_at": max((history.joined_at for history in histories), default=None),
                "history_count": len(histories),
                "created_at": entry.created_at,
            })
        return items
    async def get_history(self, db_session: AsyncSession, entry_id: int) -> Optional[dict[str, Any]]:
        return await account_pool_history_service.get_history(db_session, entry_id)


account_pool_service = AccountPoolService()
