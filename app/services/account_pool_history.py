"""账号号池的 Team 加入历史追踪。"""
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolHistory, Team, TeamEmailMapping
from app.utils.time_utils import get_now


class AccountPoolHistoryService:
    async def backfill_current_histories(
        self,
        db_session: AsyncSession,
        entries: list[AccountPoolEntry],
    ) -> None:
        if not entries:
            return
        emails = [entry.email for entry in entries]
        result = await db_session.execute(
            select(TeamEmailMapping, Team)
            .join(Team, Team.id == TeamEmailMapping.team_id)
            .where(
                TeamEmailMapping.email.in_(emails),
                TeamEmailMapping.status == "joined",
            )
        )
        entry_by_email = {entry.email: entry for entry in entries}
        for mapping, team in result.all():
            await self._record_joined_history(
                db_session=db_session,
                entry=entry_by_email[mapping.email],
                team=team,
                joined_at=mapping.joined_at or get_now(),
            )

    async def record_reconciliation(
        self,
        db_session: AsyncSession,
        *,
        team: Optional[Team],
        mappings: list[TeamEmailMapping],
        previous_states: dict[str, tuple[Optional[str], Optional[datetime]]],
        seen_at: datetime,
    ) -> None:
        if not team or not mappings:
            return
        entries = await self._load_entries(db_session, mappings)
        if not entries:
            return
        open_histories = await self._load_open_histories(db_session, team.id, entries)
        for mapping in mappings:
            entry = entries.get(mapping.email)
            if not entry:
                continue
            previous_status, _ = previous_states.get(mapping.email, (None, None))
            if mapping.status == "joined":
                await self._open_history(
                    db_session=db_session,
                    entry=entry,
                    team=team,
                    mapping=mapping,
                    previous_status=previous_status,
                    open_histories=open_histories,
                    seen_at=seen_at,
                )
            elif previous_status == "joined":
                self._close_history(entry.id, open_histories, seen_at)

    async def get_history(
        self,
        db_session: AsyncSession,
        entry_id: int,
    ) -> Optional[dict]:
        entry = await db_session.get(AccountPoolEntry, entry_id)
        if not entry or entry.deleted_at is not None:
            return None
        result = await db_session.execute(
            select(AccountPoolHistory)
            .where(AccountPoolHistory.account_pool_id == entry_id)
            .order_by(AccountPoolHistory.joined_at.desc(), AccountPoolHistory.id.desc())
        )
        return {
            "email": entry.email,
            "histories": [self._serialize_history(history) for history in result.scalars().all()],
        }

    @staticmethod
    async def _load_entries(db_session, mappings):
        emails = [mapping.email for mapping in mappings if mapping.email]
        if not emails:
            return {}
        result = await db_session.execute(
            select(AccountPoolEntry).where(AccountPoolEntry.email.in_(emails))
        )
        return {entry.email: entry for entry in result.scalars().all()}

    @staticmethod
    async def _load_open_histories(db_session, team_id, entries):
        result = await db_session.execute(
            select(AccountPoolHistory).where(
                AccountPoolHistory.account_pool_id.in_([entry.id for entry in entries.values()]),
                AccountPoolHistory.team_id == team_id,
                AccountPoolHistory.left_at.is_(None),
            )
        )
        return {history.account_pool_id: history for history in result.scalars().all()}

    async def _open_history(
        self,
        *,
        db_session,
        entry,
        team,
        mapping,
        previous_status,
        open_histories,
        seen_at,
    ) -> None:
        if previous_status == "joined" or entry.id in open_histories:
            return
        history = await self._record_joined_history(
            db_session=db_session,
            entry=entry,
            team=team,
            joined_at=mapping.joined_at or seen_at,
        )
        open_histories[entry.id] = history

    @staticmethod
    def _close_history(entry_id, open_histories, seen_at) -> None:
        history = open_histories.pop(entry_id, None)
        if history:
            history.left_at = seen_at

    @staticmethod
    async def _record_joined_history(*, db_session, entry, team, joined_at):
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

    @staticmethod
    def _serialize_history(history):
        return {
            "id": history.id,
            "team_id": history.team_id,
            "team_name": history.team_name,
            "team_email": history.team_email,
            "joined_at": history.joined_at.isoformat() if history.joined_at else None,
            "left_at": history.left_at.isoformat() if history.left_at else None,
        }


account_pool_history_service = AccountPoolHistoryService()
