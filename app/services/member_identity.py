"""Associate hidden member emails with identities from saved OAuth credentials."""
import json

from sqlalchemy import select

from app.models import AccountPoolEntry, AccountPoolWorkspace, MemberAuthorization
from app.services.openai_workspace import token_claims


def credential_identity(payload, email, account_id):
    credentials = payload["accounts"][0]["credentials"] if "accounts" in payload else payload
    claims = token_claims(credentials.get("access_token", ""))
    auth = claims.get("https://api.openai.com/auth") or {}
    profile = claims.get("https://api.openai.com/profile") or {}
    token_email = str(profile.get("email") or claims.get("email") or "").strip().lower()
    if token_email and token_email != email:
        raise ValueError("成员授权凭据的邮箱与账号不一致")
    if auth.get("chatgpt_account_id") != account_id:
        return None
    user_id = auth.get("chatgpt_user_id") or auth.get("user_id")
    return (str(user_id), email) if user_id else None


async def load_member_identities(db, account_id, *, decrypt):
    authorizations = (await db.execute(select(
        MemberAuthorization.email, MemberAuthorization.credentials_encrypted,
    ).where(
        MemberAuthorization.account_id == account_id,
        MemberAuthorization.credentials_encrypted.is_not(None),
    ))).all()
    workspaces = (await db.execute(select(
        AccountPoolEntry.email, AccountPoolWorkspace.export_json_encrypted,
    ).join(AccountPoolEntry, AccountPoolEntry.id == AccountPoolWorkspace.account_pool_id).where(
        AccountPoolWorkspace.workspace_id == account_id,
        AccountPoolWorkspace.export_json_encrypted.is_not(None),
        AccountPoolEntry.deleted_at.is_(None),
    ))).all()
    identities = {}
    for email, encrypted in (*authorizations, *workspaces):
        identity = credential_identity(json.loads(decrypt(encrypted)), email, account_id)
        if identity is None:
            continue
        user_id, email = identity
        if user_id in identities and identities[user_id] != email:
            raise ValueError("同一成员用户 ID 对应多个邮箱，无法同步成员资料")
        identities[user_id] = email
    return identities
