"""Persist the pending export of a newly invited account-pool replacement."""

from sqlalchemy import select

from app.models import MemberAuthorization, TeamEmailMapping
from app.utils.time_utils import get_now


async def mark_replacement_pending(db_session, team_id: int, email: str) -> None:
    mapping = (await db_session.execute(
        select(TeamEmailMapping).where(
            TeamEmailMapping.team_id == team_id,
            TeamEmailMapping.email == email,
        )
    )).scalar_one_or_none()
    if mapping is None:
        mapping = TeamEmailMapping(
            team_id=team_id, email=email, status="invited",
            source="auto_replacement", last_invited_at=get_now(),
        )
        db_session.add(mapping)
    mapping.replacement_export_pending = True

    authorization = (await db_session.execute(
        select(MemberAuthorization).where(
            MemberAuthorization.team_id == team_id,
            MemberAuthorization.email == email,
        )
    )).scalar_one_or_none()
    if authorization is not None:
        authorization.credentials_encrypted = None
        authorization.authorized_at = None
        authorization.export_json_encrypted = None
        authorization.export_json_updated_at = None
        authorization.sub2api_account_id = None
        authorization.sub2api_exported_at = None
        authorization.sub2api_import_uncertain = False
    await db_session.commit()
