"""成员独立授权、实时入组验证及 sub2api 数据导出。"""
import json
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select, update

from app.models import MemberAuthorization, Team
from app.services.account_pool_credentials import AccountPoolCredentialError
from app.services.encryption import encryption_service
from app.services.member_authorization_payload import build_member_export_payload
from app.services.openai_automatic_login import (
    AutomaticLoginRequest,
    OpenAIAutomaticLoginError,
)
from app.services.sub2api import apply_export_settings
from app.utils.jwt_parser import JWTParser
from app.utils.seat_lock import seat_account_lock
from app.utils.time_utils import get_now

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"


class MemberAuthorizationError(ValueError):
    pass


class MemberAuthorizationService:
    def __init__(self, team_service, automatic_login_service, credential_service):
        self.teams = team_service
        self.remote = team_service.chatgpt_service
        self.automatic_login_service = automatic_login_service
        self.credential_service = credential_service
        self.jwt = JWTParser()

    async def _team(self, team_id, db):
        team = await db.get(Team, team_id)
        if not team or not team.account_id:
            raise MemberAuthorizationError("Team 不存在或未配置工作区")
        return team

    async def _record(self, team, email, db):
        return (await db.execute(select(MemberAuthorization).where(
            MemberAuthorization.team_id == team.id, MemberAuthorization.email == email,
            MemberAuthorization.account_id == team.account_id,
        ))).scalar_one_or_none()

    async def _authorization_record(self, team, email, db):
        record = (await db.execute(select(MemberAuthorization).where(
            MemberAuthorization.team_id == team.id, MemberAuthorization.email == email,
        ))).scalar_one_or_none()
        if record is None:
            record = MemberAuthorization(
                team_id=team.id, email=email, account_id=team.account_id
            )
            db.add(record)
            return record
        if record.account_id != team.account_id:
            record.account_id = team.account_id
            record.credentials_encrypted = None
            record.authorized_at = None
            record.export_json_encrypted = None
            record.export_json_updated_at = None
            record.sub2api_account_id = None
            record.sub2api_exported_at = None
        return record

    async def _membership(self, team, email, db):
        """仅信任即时上游列表，不使用本地映射或 Token 的套餐声明判定入组。"""
        token = await self.teams.ensure_access_token(team, db)
        if not token:
            raise MemberAuthorizationError("Team 管理凭证失效，无法确认成员状态")
        result = await self.remote.get_members(token, team.account_id, db, identifier=team.email)
        if not result.get("success"):
            raise MemberAuthorizationError("成员列表读取失败，暂时无法确认入组结果")
        if any(str(m.get("email") or "").strip().lower() == email for m in result.get("members", [])):
            return "joined"
        invites = await self.remote.get_invites(token, team.account_id, db, identifier=team.email)
        if not invites.get("success"):
            raise MemberAuthorizationError("邀请列表读取失败，请稍后重新判定")
        pending = self.teams._filter_pending_invites(invites.get("items", []))
        return "invited" if any(i.get("email_address") == email for i in pending) else "absent"

    async def authorize(self, team_id, email, db):
        team = await self._team(team_id, db)
        async with seat_account_lock(team.account_id):
            if await self._membership(team, email, db) == "absent":
                raise MemberAuthorizationError("该邮箱不在当前 Team 的成员或待邀请列表中")
            record = await self._authorization_record(team, email, db)
            draft = self.remote.create_oauth_authorize_url(CLIENT_ID, REDIRECT_URI)
            record.oauth_state = draft["state"]
            record.verifier_encrypted = encryption_service.encrypt_token(draft["code_verifier"])
            record.oauth_expires_at = get_now() + timedelta(minutes=15)
            await db.commit()
            return {"authorize_url": draft["authorize_url"], "email": email, "expires_in": 900}

    def _identity(self, credentials, email):
        access = credentials.get("access_token") or ""
        claims = self.jwt.decode_token(access) if access else None
        identity = self.jwt.decode_token(credentials["id_token"]) if credentials.get("id_token") else {}
        if not claims or self.jwt.is_token_expired(access):
            raise MemberAuthorizationError("授权未返回有效的 Access Token，请重新授权")
        token_emails = {
            str(p.get("email") or (p.get("https://api.openai.com/profile") or {}).get("email") or "").strip().lower()
            for p in (claims, identity or {})
        } - {""}
        if token_emails != {email}:
            raise MemberAuthorizationError("授权登录邮箱与所选成员不一致，请使用该成员邮箱重新授权")
        return claims, identity or {}

    async def _export_payload(self, team, email, credentials, db):
        claims, identity = self._identity(credentials, email)
        payload = build_member_export_payload(
            team, email, credentials, claims, identity
        )
        return await apply_export_settings(payload, db)

    async def _save_export_json(self, record, payload, db):
        record.export_json_encrypted = encryption_service.encrypt_token(
            json.dumps(payload, ensure_ascii=False)
        )
        record.export_json_updated_at = get_now()
        await db.commit()

    async def _store_credentials(self, record, team, email, result, db):
        credentials = {
            key: result.get(key) or ""
            for key in ("access_token", "refresh_token", "id_token")
        }
        credentials["client_id"] = CLIENT_ID
        self._identity(credentials, email)
        if not credentials["refresh_token"]:
            raise MemberAuthorizationError("授权未返回 Refresh Token，请重新登录")
        record.credentials_encrypted = encryption_service.encrypt_token(
            json.dumps(credentials)
        )
        record.authorized_at = get_now()
        record.oauth_state = None
        record.verifier_encrypted = None
        record.oauth_expires_at = None
        payload = await self._export_payload(team, email, credentials, db)
        await self._save_export_json(record, payload, db)

    async def automatic_login(self, team_id, email, db):
        team = await self._team(team_id, db)
        async with seat_account_lock(team.account_id):
            if await self._membership(team, email, db) == "absent":
                raise MemberAuthorizationError("该邮箱不在当前 Team 的成员或待邀请列表中")
            try:
                account = await self.credential_service.get_credentials(db, email)
            except AccountPoolCredentialError as exc:
                raise MemberAuthorizationError(str(exc)) from exc
            if not account or not account.get("password"):
                raise MemberAuthorizationError("账号号池未保存该成员的登录密码")
            if not account.get("two_factor_secret"):
                raise MemberAuthorizationError("账号号池未保存该成员的 2FA 密钥")
            record = await self._authorization_record(team, email, db)
            await db.flush()
            draft = self.remote.create_oauth_authorize_url(CLIENT_ID, REDIRECT_URI)
            draft.update({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI})
            request = AutomaticLoginRequest(
                email=email,
                password=account["password"],
                totp_secret=account["two_factor_secret"],
                account_id=team.account_id,
                oauth_draft=draft,
                db_session=db,
                identifier=f"member-auto-login-{record.id}",
            )
            try:
                result = await self.automatic_login_service.login(request)
            except OpenAIAutomaticLoginError as exc:
                raise MemberAuthorizationError(str(exc)) from exc
            await self._store_credentials(record, team, email, result, db)
        return await self.check(team_id, email, db)

    async def callback(self, team_id, email, callback_url, db):
        team = await self._team(team_id, db)
        async with seat_account_lock(team.account_id):
            record = await self._record(team, email, db)
            if not record or not record.oauth_state or not record.oauth_expires_at or record.oauth_expires_at <= get_now():
                raise MemberAuthorizationError("授权链接已过期或已使用，请重新获取授权链接")
            parsed = urlparse(callback_url.strip())
            expected = urlparse(REDIRECT_URI)
            if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path) or parsed.fragment:
                raise MemberAuthorizationError("请粘贴本次授权的完整 localhost:1455/auth/callback 回调 URL")
            query = parse_qs(parsed.query, keep_blank_values=True)
            if len(query.get("state", [])) != 1 or not secrets.compare_digest(query["state"][0], record.oauth_state):
                raise MemberAuthorizationError("授权 state 缺失或不匹配，请使用本次链接的回调")
            if query.get("error"):
                raise MemberAuthorizationError("用户未完成授权，请重新获取链接并授权")
            if len(query.get("code", [])) != 1 or not query["code"][0]:
                raise MemberAuthorizationError("回调中缺少有效授权码")
            verifier = encryption_service.decrypt_token(record.verifier_encrypted)
            # 在兑换前原子消费，防止重放或多个 worker 同时兑换同一授权码。
            consumed = await db.execute(update(MemberAuthorization).where(
                MemberAuthorization.id == record.id, MemberAuthorization.oauth_state == record.oauth_state,
            ).values(oauth_state=None, verifier_encrypted=None, oauth_expires_at=None))
            await db.commit()
            if consumed.rowcount != 1:
                raise MemberAuthorizationError("此授权回调已处理，请重新获取链接")
            result = await self.remote.exchange_oauth_code(
                code=query["code"][0], client_id=CLIENT_ID, redirect_uri=REDIRECT_URI,
                code_verifier=verifier, db_session=db, identifier=f"member-oauth-{record.id}",
            )
            if not result.get("success"):
                raise MemberAuthorizationError("授权码兑换失败，请重新获取授权链接后重试")
            await self._store_credentials(record, team, email, result, db)
        return await self.check(team_id, email, db)

    async def _credentials(self, record, email, db):
        try:
            credentials = json.loads(encryption_service.decrypt_token(record.credentials_encrypted))
        except Exception:
            raise MemberAuthorizationError("授权信息不可用，请重新授权") from None
        if self.jwt.is_token_expired(credentials.get("access_token") or ""):
            result = await self.remote.refresh_access_token_with_refresh_token(
                credentials.get("refresh_token") or "", credentials.get("client_id") or CLIENT_ID,
                db, identifier=f"member-oauth-{record.id}",
            )
            if not result.get("success") or not result.get("access_token"):
                raise MemberAuthorizationError("成员授权已失效，请重新授权后再导出")
            for key in ("access_token", "refresh_token", "id_token"):
                if result.get(key):
                    credentials[key] = result[key]
            self._identity(credentials, email)
            record.credentials_encrypted = encryption_service.encrypt_token(json.dumps(credentials))
            team = await db.get(Team, record.team_id)
            payload = await self._export_payload(team, email, credentials, db)
            await self._save_export_json(record, payload, db)
        self._identity(credentials, email)
        if not credentials.get("refresh_token"):
            raise MemberAuthorizationError("缺少 Refresh Token，请重新授权")
        return credentials

    async def _check(self, team, email, db):
        record = await self._record(team, email, db)
        data = {"email": email, "account_id": team.account_id,
                "authorized": bool(record and record.credentials_encrypted),
                "json_saved": bool(record and record.export_json_encrypted),
                "json_updated_at": record.export_json_updated_at.isoformat() if record and record.export_json_updated_at else None,
                "sub2api_exported": bool(record and record.sub2api_exported_at),
                "sub2api_exported_at": record.sub2api_exported_at.isoformat() if record and record.sub2api_exported_at else None,
                "sub2api_account_id": record.sub2api_account_id if record else None,
                "authorization_pending": bool(record and record.oauth_state and record.oauth_expires_at and record.oauth_expires_at > get_now()),
                "membership": "unknown", "can_export": False}
        credentials = None
        try:
            # 判定、持久化人数与页面刷新共用一次上游快照，避免第二次读取显示旧状态。
            snapshot = await self.teams.get_team_members(team.id, db)
            if not snapshot.get("success"):
                message = snapshot.get("error") if snapshot.get("owner_authorization_error") else None
                raise MemberAuthorizationError(message or "成员列表读取失败，暂时无法确认入组结果")
            data["members_snapshot"] = snapshot
            member = next((m for m in snapshot["members"] if m["email"] == email), None)
            if member:
                data["membership"] = member["status"]
            elif not snapshot["seat_summary"]["invites_complete"]:
                raise MemberAuthorizationError("邀请列表读取失败，请稍后重新判定")
            else:
                data["membership"] = "absent"
            if not data["authorized"]:
                data["message"] = "尚未授权，请先获取授权链接并完成回调解析"
            elif data["membership"] != "joined":
                data["message"] = "已授权，等待成员接受邀请后重新判定" if data["membership"] == "invited" else "成员已不在当前 Team，无法导出"
            else:
                credentials = await self._credentials(record, email, db)
                data["can_export"] = True
                data["message"] = "已确认加入当前 Team，可以导出 sub2api JSON"
        except MemberAuthorizationError as exc:
            data["message"] = str(exc)
        return data, credentials

    async def check(self, team_id, email, db):
        team = await self._team(team_id, db)
        async with seat_account_lock(team.account_id):
            data, _ = await self._check(team, email, db)
            return data

    async def export(self, team_id, email, db):
        team = await self._team(team_id, db)
        async with seat_account_lock(team.account_id):
            data, credentials = await self._check(team, email, db)
            if not data["can_export"]:
                raise MemberAuthorizationError(data["message"])
            payload = await self._export_payload(team, email, credentials, db)
            record = await self._record(team, email, db)
            await self._save_export_json(record, payload, db)
            return payload

    async def mark_sub2api_exported(self, team_id, email, account_id, db):
        team = await self._team(team_id, db)
        record = await self._record(team, email, db)
        if not record or not record.export_json_encrypted:
            raise MemberAuthorizationError("授权 JSON 状态不存在，请重新授权后再导出")
        record.sub2api_account_id = account_id
        record.sub2api_exported_at = get_now()
        await db.commit()
