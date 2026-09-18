"""后台账号号池及成员加入历史服务。"""
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolHistory, Team, TeamEmailMapping
from app.utils.time_utils import get_now
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACTIVE_MAPPING_STATUSES = ("invited", "joined")
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
            if email in existing:
                continue
            entry = AccountPoolEntry(email=email)
            db_session.add(entry)
            existing[email] = entry
            added.append(email)

        await db_session.flush()
        await self.backfill_current_histories(db_session, list(existing.values()))
        await db_session.commit()
        return {
            "success": bool(added or existing),
            "message": f"新增 {len(added)} 个邮箱，已存在 {len(normalized) - len(added)} 个",
            "added": added,
            "existing": [email for email in normalized if email in existing and email not in added],
            "invalid": invalid,
        }
    async def backfill_current_histories(
        self,
        db_session: AsyncSession,
        entries: list[AccountPoolEntry],
    ) -> None:
        if not entries:
            return
        emails = [entry.email for entry in entries]
        mappings_result = await db_session.execute(
            select(TeamEmailMapping, Team)
            .join(Team, Team.id == TeamEmailMapping.team_id)
            .where(
                TeamEmailMapping.email.in_(emails),
                TeamEmailMapping.status == "joined",
            )
        )
        entry_by_email = {entry.email: entry for entry in entries}
        for mapping, team in mappings_result.all():
            await self._record_joined_history(
                db_session,
                entry_by_email[mapping.email],
                team,
                mapping.joined_at or get_now(),
            )
    async def record_reconciliation(
        self,
        db_session: AsyncSession,
        team: Optional[Team],
        mappings: list[TeamEmailMapping],
        previous_states: dict[str, tuple[Optional[str], Optional[datetime]]],
        seen_at: datetime,
    ) -> None:
        if not team or not mappings:
            return
        emails = [mapping.email for mapping in mappings if mapping.email]
        if not emails:
            return
        entries_result = await db_session.execute(
            select(AccountPoolEntry).where(AccountPoolEntry.email.in_(emails))
        )
        entries = {entry.email: entry for entry in entries_result.scalars().all()}
        if not entries:
            return

        history_result = await db_session.execute(
            select(AccountPoolHistory).where(
                AccountPoolHistory.account_pool_id.in_([entry.id for entry in entries.values()]),
                AccountPoolHistory.team_id == team.id,
                AccountPoolHistory.left_at.is_(None),
            )
        )
        open_histories = {history.account_pool_id: history for history in history_result.scalars().all()}
        for mapping in mappings:
            entry = entries.get(mapping.email)
            if not entry:
                continue
            previous_status, _ = previous_states.get(mapping.email, (None, None))
            current_status = mapping.status
            if current_status == "joined":
                if previous_status != "joined" and entry.id not in open_histories:
                    history = await self._record_joined_history(
                        db_session,
                        entry,
                        team,
                        mapping.joined_at or seen_at,
                    )
                    open_histories[entry.id] = history
            elif previous_status == "joined":
                history = open_histories.get(entry.id)
                if history:
                    history.left_at = seen_at
                    open_histories.pop(entry.id, None)
    async def _record_joined_history(
        self,
        db_session: AsyncSession,
        entry: AccountPoolEntry,
        team: Team,
        joined_at: datetime,
    ) -> AccountPoolHistory:
        result = await db_session.execute(
            select(AccountPoolHistory).where(
                AccountPoolHistory.account_pool_id == entry.id,
                AccountPoolHistory.team_id == team.id,
                AccountPoolHistory.left_at.is_(None),
            )
        )
        history = result.scalar_one_or_none()
        if history:
            return history
        history = AccountPoolHistory(
            account_pool_id=entry.id,
            team_id=team.id,
            team_name=team.team_name,
            team_email=team.email,
            joined_at=joined_at,
        )
        db_session.add(history)
        return history
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
            histories = histories_by_entry.get(entry.id, [])
            items.append({
                "id": entry.id,
                "email": entry.email,
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
        entry = await db_session.get(AccountPoolEntry, entry_id)
        if not entry:
            return None
        result = await db_session.execute(
            select(AccountPoolHistory)
            .where(AccountPoolHistory.account_pool_id == entry_id)
            .order_by(AccountPoolHistory.joined_at.desc(), AccountPoolHistory.id.desc())
        )
        return {
            "email": entry.email,
            "histories": [
                {
                    "id": history.id,
                    "team_id": history.team_id,
                    "team_name": history.team_name,
                    "team_email": history.team_email,
                    "joined_at": history.joined_at.isoformat() if history.joined_at else None,
                    "left_at": history.left_at.isoformat() if history.left_at else None,
                }
                for history in result.scalars().all()
            ],
        }
account_pool_service = AccountPoolService()
