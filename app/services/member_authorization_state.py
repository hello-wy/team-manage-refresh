"""Credential identity checks and persisted authorization status."""
from app.utils.time_utils import get_now


class MemberAuthorizationError(ValueError):
    pass


def decode_identity(credentials, email, *, jwt):
    access = credentials.get("access_token") or ""
    claims = jwt.decode_token(access) if access else None
    identity = jwt.decode_token(credentials["id_token"]) if credentials.get("id_token") else {}
    if not claims or jwt.is_token_expired(access):
        raise MemberAuthorizationError("授权未返回有效的 Access Token，请重新授权")
    token_emails = {
        str(p.get("email") or (p.get("https://api.openai.com/profile") or {}).get("email") or "").strip().lower()
        for p in (claims, identity or {})
    } - {""}
    if token_emails != {email}:
        raise MemberAuthorizationError("授权登录邮箱与所选成员不一致，请使用该成员邮箱重新授权")
    return claims, identity or {}


def authorization_state(record, account_id):
    updated_at = record.export_json_updated_at if record else None
    exported_at = record.sub2api_exported_at if record else None
    return {
        "authorized": bool(record and record.credentials_encrypted),
        "json_saved": bool(record and record.export_json_encrypted),
        "json_updated_at": updated_at.isoformat() if updated_at else None,
        "sub2api_exported": bool(record and record.sub2api_exported_at),
        "sub2api_exported_at": exported_at.isoformat() if exported_at else None,
        "sub2api_account_id": record.sub2api_account_id if record else None,
        "workspace_verified": bool(
            record and record.workspace_id == str(account_id)
            and record.workspace_status == "workspace_ok"
        ),
        "authorization_pending": bool(
            record and record.oauth_state and record.oauth_expires_at
            and record.oauth_expires_at > get_now()
        ),
    }
