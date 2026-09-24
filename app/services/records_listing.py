"""Build the Sub2API export list and fetch quota only for visible rows."""
import math
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Sub2apiExportRecord, TeamEmailMapping
from app.utils.seats import normalize_seat_type
from app.utils.time_utils import get_now


async def _load_candidates(db: AsyncSession, options: dict[str, Any]):
    query = select(Sub2apiExportRecord).order_by(
        Sub2apiExportRecord.last_exported_at.desc()
    )
    if options["search"]:
        pattern = f"%{options['search']}%"
        query = query.where(or_(
            Sub2apiExportRecord.email.ilike(pattern),
            Sub2apiExportRecord.team_name.ilike(pattern),
            Sub2apiExportRecord.team_space_id.ilike(pattern),
        ))
    paginated = options["joined_filter"] == "all" and not options["usage_filter"]
    total = 0
    page = options["page"]
    if paginated:
        count_query = query.with_only_columns(
            func.count(Sub2apiExportRecord.id)
        ).order_by(None)
        total = int((await db.execute(count_query)).scalar() or 0)
        pages = max(1, math.ceil(total / options["per_page"]))
        page = max(1, min(page, pages))
        query = query.limit(options["per_page"]).offset(
            (page - 1) * options["per_page"]
        )
    candidates = list((await db.execute(query)).scalars().all())
    team_ids = {row.team_id for row in candidates if row.team_id}
    emails = {row.email for row in candidates if row.team_id}
    mappings = {}
    if team_ids:
        result = await db.execute(select(TeamEmailMapping).where(
            TeamEmailMapping.team_id.in_(team_ids),
            TeamEmailMapping.email.in_(emails),
            TeamEmailMapping.status == "joined",
        ))
        mappings = {(row.team_id, row.email): row for row in result.scalars().all()}
    cutoff = get_now() - timedelta(days=7)
    visible = []
    for row in candidates:
        mapping = mappings.get((row.team_id, row.email))
        if mapping:
            seat = normalize_seat_type(mapping.seat_type)
            if seat != "unknown":
                row.seat_type = seat
            row.joined_at = mapping.joined_at or row.joined_at
        if options["joined_filter"] == "within_7_days" and (
            row.joined_at is None or row.joined_at < cutoff
        ):
            continue
        visible.append(row)
    return visible, total if paginated else len(visible), page, paginated


def _record_view(row: Sub2apiExportRecord, usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.id,
        "email": row.email,
        "team_space_id": row.team_space_id,
        "team_name": row.team_name or "-",
        "seat_type": row.seat_type or "unknown",
        "joined_at": row.joined_at.strftime("%Y-%m-%d %H:%M:%S") if row.joined_at else "-",
        "team_5x_completed": row.team_5x_completed,
        "premium_quota_exhausted": row.premium_quota_exhausted,
        "export_count": row.export_count,
        "last_exported_at": row.last_exported_at.strftime("%Y-%m-%d %H:%M:%S")
        if row.last_exported_at else "-",
        "usage": usage,
    }


async def _enrich_records(db: AsyncSession, rows, usage_filter: str, usage_service):
    keys = [(row.email, row.team_space_id) for row in rows]
    usage_by_account = await usage_service.check_many(db, keys)
    enriched = []
    quota_updated = False
    for row in rows:
        usage = usage_by_account.get(
            (row.email, row.team_space_id),
            {"status": "unavailable", "error": "账号池没有可用 JSON"},
        )
        weekly_state = (usage.get("1week") or {}).get("state")
        if usage_filter and weekly_state != usage_filter:
            continue
        if usage.get("status") == "ok" and weekly_state in {"exhausted", "available"}:
            completed = weekly_state == "exhausted"
            row.team_5x_completed = completed
            row.premium_quota_exhausted = completed and row.seat_type == "premium"
            row.quota_checked_at = get_now()
            quota_updated = True
        enriched.append(_record_view(row, usage))
    if quota_updated:
        await db.commit()
    return enriched


async def list_export_records(
    db: AsyncSession, options: dict[str, Any], usage_service: Any
) -> dict[str, Any]:
    candidates, total, page, paginated = await _load_candidates(db, options)
    per_page = options["per_page"]
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    if options["usage_filter"]:
        rows = await _enrich_records(
            db, candidates, options["usage_filter"], usage_service
        )
        total = len(rows)
        total_pages = max(1, math.ceil(total / per_page))
        page = max(1, min(page, total_pages))
        rows = rows[(page - 1) * per_page:page * per_page]
    else:
        selected = candidates if paginated else candidates[
            (page - 1) * per_page:page * per_page
        ]
        rows = await _enrich_records(db, selected, "", usage_service)
    return {
        "records": rows,
        "pagination": {
            "current_page": page,
            "total_pages": total_pages,
            "total": total,
            "per_page": per_page,
        },
    }
