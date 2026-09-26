"""Persist deduplicated successful Sub2API export records."""
from __future__ import annotations

from datetime import datetime
from typing import Any
import inspect

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Sub2apiExportRecord, Team, TeamEmailMapping
from app.utils.seats import normalize_seat_type
from app.utils.time_utils import get_now


class Sub2apiExportRecordService:
    """Upsert one row per normalized email and Team workspace."""

    async def delete_for_emails(self, db: AsyncSession, emails: list[str]) -> int:
        """Remove every export record owned by the deleted account emails."""
        normalized_emails = {
            str(email or "").strip().lower() for email in emails if str(email or "").strip()
        }
        if not normalized_emails:
            return 0
        result = await db.execute(
            delete(Sub2apiExportRecord).where(
                Sub2apiExportRecord.email.in_(normalized_emails)
            )
        )
        return int(result.rowcount or 0)

    async def record_success(
        self,
        db: AsyncSession,
        *,
        email: str,
        team_space_id: str,
        sub2api_account_id: int | None = None,
        team_id: int | None = None,
        team_name: str | None = None,
        team_email: str | None = None,
    ) -> Sub2apiExportRecord | None:
        normalized_email = str(email or "").strip().lower()
        normalized_space = str(team_space_id or "").strip()
        if not normalized_email or not normalized_space:
            return None

        if team_id is not None and (not team_name or not team_email):
            team = await db.get(Team, team_id)
            if team:
                team_name = team_name or team.team_name
                team_email = team_email or team.email
                team_space_id = normalized_space

        seat_type = None
        joined_at = None
        if team_id is not None:
            mapping_result = await db.execute(
                select(TeamEmailMapping).where(
                    TeamEmailMapping.team_id == team_id,
                    TeamEmailMapping.email == normalized_email,
                    TeamEmailMapping.status == "joined",
                )
            )
            mapping = mapping_result.scalar_one_or_none()
            if inspect.isawaitable(mapping):
                mapping = await mapping
            if isinstance(mapping, TeamEmailMapping):
                normalized_seat = normalize_seat_type(mapping.seat_type)
                seat_type = normalized_seat if normalized_seat != "unknown" else None
                joined_at = mapping.joined_at

        now = get_now()
        result = await db.execute(
            select(Sub2apiExportRecord).where(
                Sub2apiExportRecord.email == normalized_email,
                Sub2apiExportRecord.team_space_id == normalized_space,
            )
        )
        record = result.scalar_one_or_none()
        if inspect.isawaitable(record):
            record = await record
        if record is None:
            record = Sub2apiExportRecord(
                email=normalized_email,
                team_space_id=normalized_space,
                team_id=team_id,
                team_name=team_name,
                team_email=team_email,
                sub2api_account_id=sub2api_account_id,
                seat_type=seat_type,
                joined_at=joined_at,
                export_count=1,
                first_exported_at=now,
                last_exported_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(record)
        else:
            record.team_id = team_id if team_id is not None else record.team_id
            record.team_name = team_name or record.team_name
            record.team_email = team_email or record.team_email
            record.sub2api_account_id = sub2api_account_id
            record.seat_type = seat_type or record.seat_type
            record.joined_at = joined_at or record.joined_at
            record.export_count = int(record.export_count or 0) + 1
            record.last_exported_at = now
            record.updated_at = now
        await db.flush()
        return record


sub2api_export_record_service = Sub2apiExportRecordService()
