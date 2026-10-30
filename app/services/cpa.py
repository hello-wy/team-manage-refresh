"""Convert verified OAuth exports to CPA Codex auth files and upload them."""
import json
import re
from datetime import datetime

import httpx

EXPECTED_EXPORT_ACCOUNTS = 1
REQUIRED_CREDENTIAL_FIELDS = ("access_token", "refresh_token", "email", "expires_at")


class CpaExportError(ValueError):
    pass


def _oauth_account(payload):
    if not isinstance(payload, dict):
        raise CpaExportError("CPA 导出数据必须是 JSON 对象")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or len(accounts) != EXPECTED_EXPORT_ACCOUNTS:
        raise CpaExportError("CPA 导出需要一个已授权的 OAuth 账号")
    account = accounts[0]
    if not isinstance(account, dict):
        raise CpaExportError("CPA 账号数据必须是 JSON 对象")
    if account.get("platform") != "openai" or account.get("type") != "oauth":
        raise CpaExportError("CPA 仅支持 OpenAI OAuth 账号")
    return account


def _has_text(value):
    return isinstance(value, str) and bool(value.strip())


def _credentials(account):
    credentials = account.get("credentials")
    if not isinstance(credentials, dict):
        raise CpaExportError("CPA 导出缺少 OAuth 凭证")
    missing = [key for key in REQUIRED_CREDENTIAL_FIELDS if not _has_text(credentials.get(key))]
    if missing:
        raise CpaExportError(f"CPA 导出缺少凭证字段：{'、'.join(missing)}")
    try:
        datetime.fromisoformat(credentials["expires_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise CpaExportError("CPA 凭证过期时间格式无效") from exc
    return credentials


def build_auth_file(payload):
    credentials = _credentials(_oauth_account(payload))
    exported_at = payload.get("exported_at")
    if not _has_text(exported_at):
        raise CpaExportError("CPA 导出缺少凭证更新时间")
    email = credentials["email"].strip().lower()
    workspace_id = str(credentials.get("chatgpt_account_id") or "")
    auth_file = {
        "type": "codex", "email": email, "account_id": workspace_id,
        "access_token": credentials["access_token"],
        "refresh_token": credentials["refresh_token"],
        "id_token": credentials.get("id_token") or "",
        "client_id": credentials.get("client_id") or "",
        "expired": credentials["expires_at"], "last_refresh": exported_at,
    }
    safe_email = re.sub(r"[^a-zA-Z0-9@._-]", "_", email)
    safe_workspace = re.sub(r"[^a-zA-Z0-9_-]", "_", workspace_id) if workspace_id else "personal"
    return f"codex-{safe_email}-{safe_workspace}.json", auth_file


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
            raise CpaExportError("请先在系统设置的 CliproxyAPI 推送中填写 CPA 地址和管理密钥")
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
            raise CpaExportError(f"CPA 上传失败（HTTP {exc.response.status_code}）：{exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise CpaExportError(f"CPA 请求失败，上传结果未确认：{exc}") from exc
        return {"filename": filename, "email": auth_file["email"]}
