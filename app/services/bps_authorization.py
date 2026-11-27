"""Obtain a fresh BPS grant without modifying Codex / quota credentials."""
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, select

from app.models import AccountPoolEntry, Team
from app.services.account_pool_authorization import AccountPoolAuthorizationService
from app.services.account_pool_authorization_models import AccountPoolAuthorizationError
from app.services.openai_automatic_login_models import AutomaticLoginRequest
from app.services.openai_oauth_clients import BPS_CLIENT_ID, BPS_REDIRECT_URI, BPS_SCOPE


class BpsAccountPoolAuthorizationService(AccountPoolAuthorizationService):
    client_id = BPS_CLIENT_ID

    def _request(self, entry, credentials, workspace_id, session):
        draft = self._auth_client.create_oauth_authorize_url(
            BPS_CLIENT_ID, BPS_REDIRECT_URI, scope=BPS_SCOPE,
            codex_cli_simplified_flow=False, id_token_add_organizations=False,
        )
        url = urlsplit(draft["authorize_url"])
        query = [(key, value) for key, value in parse_qsl(url.query)
                 if key not in {"codex_cli_simplified_flow", "id_token_add_organizations", "prompt"}]
        draft["authorize_url"] = urlunsplit(url._replace(query=urlencode(query)))
        draft.update(client_id=BPS_CLIENT_ID, redirect_uri=BPS_REDIRECT_URI)
        return AutomaticLoginRequest(
            email=entry.email, password=credentials["password"],
            totp_secret=credentials["two_factor_secret"],
            account_id=str(workspace_id or "").strip(), oauth_draft=draft,
            db_session=session, identifier=f"account-pool-bps-{entry.id}",
            read_subscription=False,
        )

    @staticmethod
    def _validate_tokens(result, claims):
        AccountPoolAuthorizationService._validate_tokens(result, claims)
        if claims.get("client_id") != BPS_CLIENT_ID:
            raise AccountPoolAuthorizationError("登录未返回 BPS 专用授权，无法导出")

    async def save_result(self, *args, **kwargs):
        raise AccountPoolAuthorizationError("BPS 凭据不能写入 Codex 凭据缓存")


async def login_for_bps_export(authorization, session, entry, workspace_id):
    if entry is None or entry.deleted_at is not None:
        raise AccountPoolAuthorizationError("账号不存在")
    version = (entry.email, entry.password_encrypted, entry.two_factor_secret_encrypted)
    result = await authorization.login_entry(session, entry.id, workspace_id)
    await session.refresh(entry)
    if entry.deleted_at is not None or version != (entry.email, entry.password_encrypted, entry.two_factor_secret_encrypted):
        raise AccountPoolAuthorizationError("登录期间账号凭据已变更，请重新登录")
    return result


@dataclass(frozen=True)
class BpsOwnerExport:
    payload: dict
    relogin_attempted: bool = True


class TeamBpsExportPreparation:
    def __init__(self, authorization):
        self._authorization = authorization

    async def prepare(self, team_id, db):
        team = await db.get(Team, team_id)
        if team is None or not team.account_id:
            raise AccountPoolAuthorizationError("Team 不存在或缺少工作区信息")
        email, workspace_id = team.email.strip().lower(), team.account_id
        entry = await db.scalar(select(AccountPoolEntry).where(
            func.lower(func.trim(AccountPoolEntry.email)) == email,
            AccountPoolEntry.deleted_at.is_(None),
        ))
        if entry is None:
            raise AccountPoolAuthorizationError("请先在账号号池保存母号的密码与 2FA，再导出 BPS")
        result = await login_for_bps_export(self._authorization, db, entry, workspace_id)
        await db.refresh(team)
        if team.account_id != workspace_id or team.email.strip().lower() != email:
            raise AccountPoolAuthorizationError("登录期间 Team 身份已变化，未导出")
        return BpsOwnerExport(result.payload)
