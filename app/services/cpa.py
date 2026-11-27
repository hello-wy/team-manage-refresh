"""Convert verified OAuth exports to CPA BPS auth files and upload them."""
import json
import hashlib
from datetime import datetime, timezone

from app.services.openai_oauth_clients import BPS_CLIENT_ID
from app.services.openai_workspace import token_claims, token_workspace_id

import httpx

EXPECTED_EXPORT_ACCOUNTS = 1
REQUIRED_CREDENTIAL_FIELDS = ("access_token", "refresh_token", "email", "expires_at")


class CpaExportError(ValueError):
    pass


def _oauth_account(payload):
    if not isinstance(payload, dict):
        raise CpaExportError("CPA BPS 导出数据必须是 JSON 对象")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or len(accounts) != EXPECTED_EXPORT_ACCOUNTS:
        raise CpaExportError("CPA BPS 导出需要一个已授权的 OAuth 账号")
    account = accounts[0]
    if not isinstance(account, dict):
        raise CpaExportError("CPA BPS 账号数据必须是 JSON 对象")
    if account.get("platform") != "openai" or account.get("type") != "oauth":
        raise CpaExportError("CPA BPS 仅支持 OpenAI OAuth 账号")
    return account


def _has_text(value):
    return isinstance(value, str) and bool(value.strip())


def _credentials(account):
    credentials = account.get("credentials")
    if not isinstance(credentials, dict):
        raise CpaExportError("CPA BPS 导出缺少 OAuth 凭证")
    missing = [key for key in REQUIRED_CREDENTIAL_FIELDS if not _has_text(credentials.get(key))]
    if missing:
        raise CpaExportError(f"CPA BPS 导出缺少凭证字段：{'、'.join(missing)}")
    try:
        datetime.fromisoformat(credentials["expires_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise CpaExportError("CPA BPS 凭证过期时间格式无效") from exc
    return credentials


def build_auth_file(payload):
    credentials = _credentials(_oauth_account(payload))
    exported_at = payload.get("exported_at")
    if not _has_text(exported_at):
        raise CpaExportError("CPA BPS 导出缺少凭证更新时间")
    email = credentials["email"].strip().lower()
    claims = token_claims(credentials["access_token"])
    identity = token_claims(credentials.get("id_token") or "")
    if credentials.get("client_id") != BPS_CLIENT_ID or claims.get("client_id") != BPS_CLIENT_ID:
        raise CpaExportError("需要独立的 BPS OAuth 授权，不能使用 Codex 凭据")
    workspace_id = str(credentials.get("chatgpt_account_id") or "").strip()
    token_workspace = token_workspace_id(credentials["access_token"]) or token_workspace_id(credentials.get("id_token") or "")
    if not workspace_id or (token_workspace and token_workspace != workspace_id):
        raise CpaExportError("BPS 凭据与所选工作区不一致")
    profile = claims.get("https://api.openai.com/profile")
    token_email = identity.get("email") or (profile.get("email") if isinstance(profile, dict) else None)
    if token_email and str(token_email).strip().lower() != email:
        raise CpaExportError("BPS 凭据与账号邮箱不一致")
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise CpaExportError("BPS 凭据缺少用户身份")
    expiry = datetime.fromisoformat(credentials["expires_at"].replace("Z", "+00:00"))
    token_expiry = claims.get("exp")
    if not isinstance(token_expiry, (int, float)) or token_expiry <= datetime.now(timezone.utc).timestamp():
        raise CpaExportError("BPS Access Token 已过期或缺少有效期")
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise CpaExportError("BPS 凭据已过期或过期时间缺少时区")
    auth_file = {
        "type": "bps", "email": email, "account_id": workspace_id, "sub": subject,
        "access_token": credentials["access_token"],
        "refresh_token": credentials["refresh_token"],
        "id_token": credentials.get("id_token") or "",
        "client_id": BPS_CLIENT_ID, "token_type": "Bearer",
        "expired": credentials["expires_at"], "last_refresh": exported_at,
    }
    # Match the CPA BPS provider identity, so repeated exports replace one record.
    digest = hashlib.sha256((subject + "\x00" + workspace_id + "\x00" + BPS_CLIENT_ID).encode()).hexdigest()[:32]
    return f"bps-{digest}.json", auth_file


class CpaService:
    MANAGEMENT_PATH = "/v0/management/auth-files"
    REQUEST_TIMEOUT_SECONDS = 20

    def __init__(self, *, load_config, client_factory):
        self._load_config = load_config
        self._client_factory = client_factory

    async def import_member(self, payload, db):
        filename, auth_file = build_auth_file(payload)
        config = await self._load_config(db)
        if config is None:
            raise CpaExportError("请先在系统设置的 CliproxyAPI 推送中填写 CPA BPS 地址和管理密钥")
        headers = {"Authorization": f"Bearer {config.api_key}", "Accept": "application/json"}
        try:
            async with self._client_factory(
                timeout=self.REQUEST_TIMEOUT_SECONDS, headers=headers, proxy=config.proxy,
            ) as client:
                response = await client.post(
                    f"{config.base_url.rstrip('/')}{self.MANAGEMENT_PATH}",
                    params={"name": filename},
                    content=json.dumps(auth_file, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CpaExportError(f"CPA BPS 上传失败（HTTP {exc.response.status_code}）") from exc
        except httpx.RequestError as exc:
            raise CpaExportError(f"CPA BPS 请求失败，上传结果未确认（{type(exc).__name__}）") from exc
        return {"filename": filename, "email": auth_file["email"]}
