"""Keep a Team owner's member authorization in sync with Team credentials."""
import json

from sqlalchemy import select

from app.models import MemberAuthorization
from app.services.encryption import encryption_service
from app.services.member_authorization_payload import build_member_export_payload
from app.services.sub2api import apply_export_settings
from app.utils.jwt_parser import JWTParser
from app.utils.time_utils import get_now


class TeamOwnerAuthorizationError(ValueError):
    pass


async def sync_team_owner_authorization(team, access_token, members, db):
    email = team.email.strip().lower()
    owner = next((member for member in members
                  if str(member.get("email") or "").strip().lower() == email
                  and member.get("role") == "account-owner"), None)
    if owner is None or not team.refresh_token_encrypted:
        return

    jwt = JWTParser()
    claims = jwt.decode_token(access_token)
    if not claims or jwt.is_token_expired(access_token):
        raise TeamOwnerAuthorizationError("母号 Access Token 无效，无法同步成员授权")
    token_email = str(jwt.extract_email(access_token) or "").strip().lower()
    if token_email != email:
        raise TeamOwnerAuthorizationError("母号 Token 邮箱与 Team 所有者不一致，无法同步授权")

    id_token = (encryption_service.decrypt_token(team.id_token_encrypted)
                if team.id_token_encrypted else "")
    identity = jwt.decode_token(id_token) if id_token else {}
    if id_token and str(jwt.extract_email(id_token) or "").strip().lower() != email:
        raise TeamOwnerAuthorizationError("母号 ID Token 邮箱与 Team 所有者不一致，无法同步授权")
    credentials = {
        "access_token": access_token,
        "refresh_token": encryption_service.decrypt_token(team.refresh_token_encrypted),
        "id_token": id_token,
        "client_id": team.client_id or "",
    }
    if not credentials["refresh_token"]:
        raise TeamOwnerAuthorizationError("母号缺少 Refresh Token，无法同步成员授权")

    record = (await db.execute(select(MemberAuthorization).where(
        MemberAuthorization.team_id == team.id, MemberAuthorization.email == email,
    ))).scalar_one_or_none()
    if record and record.account_id == team.account_id and record.credentials_encrypted:
        previous = json.loads(encryption_service.decrypt_token(record.credentials_encrypted))
        if previous == credentials and record.export_json_encrypted:
            return

    payload = build_member_export_payload(team, email, credentials, claims, identity or {})
    payload = await apply_export_settings(payload, db)
    if record is None:
        record = MemberAuthorization(team_id=team.id, email=email, account_id=team.account_id)
        db.add(record)
    if record.account_id != team.account_id:
        record.sub2api_account_id = None
        record.sub2api_exported_at = None
    record.account_id = team.account_id
    record.credentials_encrypted = encryption_service.encrypt_token(json.dumps(credentials))
    record.export_json_encrypted = encryption_service.encrypt_token(json.dumps(payload))
    record.authorized_at = get_now()
    record.export_json_updated_at = get_now()
    await db.commit()
