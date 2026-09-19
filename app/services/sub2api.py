"""Push an authorized member's sub2api account to the configured instance."""
from urllib.parse import urlparse

import httpx

from app.services.encryption import encryption_service
from app.services.settings import settings_service

DEFAULT_BASE_URL = "https://solidapi.top"
REQUEST_TIMEOUT_SECONDS = 20.0


class Sub2apiError(ValueError):
    pass


def normalize_base_url(value: str) -> str:
    url = (value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise Sub2apiError("sub2api Base URL 必须是 http(s) 站点根地址")
    return url


def response_data(response: httpx.Response):
    if not response.is_success:
        raise Sub2apiError(f"sub2api 请求失败 (HTTP {response.status_code})，请检查地址和密钥")
    try:
        body = response.json()
    except ValueError as exc:
        raise Sub2apiError("sub2api 返回的内容不是 JSON") from exc
    if not isinstance(body, dict) or body.get("code") != 0 or "data" not in body:
        raise Sub2apiError("sub2api 拒绝请求，请检查配置及账户数据")
    return body["data"]


class Sub2apiService:
    def __init__(self, client_factory=httpx.AsyncClient):
        self.client_factory = client_factory

    async def import_member(self, payload, db):
        base_url = normalize_base_url(await settings_service.get_setting(
            db, "sub2api_base_url", DEFAULT_BASE_URL))
        encrypted = await settings_service.get_setting(db, "sub2api_api_key_encrypted", "")
        if not encrypted:
            raise Sub2apiError("请先在系统中心设置 sub2api x-api-key")
        api_key = encryption_service.decrypt_token(encrypted)
        headers = {"x-api-key": api_key}
        try:
            async with self.client_factory(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers) as client:
                groups_response = await client.get(f"{base_url}/api/v1/admin/groups/all",
                                                   params={"platform": "openai"})
                groups = response_data(groups_response)
                if not isinstance(groups, list):
                    raise Sub2apiError("sub2api 分组响应格式错误")
                group_ids = [group["id"] for group in groups
                             if isinstance(group, dict) and group.get("platform") == "openai"
                             and isinstance(group.get("id"), int) and group["id"] > 0]
                if not group_ids:
                    raise Sub2apiError("sub2api 没有可用的 OpenAI 分组，未创建账户")
                account = {**payload["accounts"][0], "platform": "openai", "group_ids": group_ids}
                created = response_data(await client.post(f"{base_url}/api/v1/admin/accounts", json=account))
        except httpx.RequestError as exc:
            raise Sub2apiError("无法连接 sub2api，请检查站点地址与网络") from exc
        if not isinstance(created, dict) or not isinstance(created.get("id"), int):
            raise Sub2apiError("sub2api 创建响应缺少账户 ID，请到目标站点核对是否已创建")
        return {"account_id": created["id"], "group_count": len(group_ids)}


sub2api_service = Sub2apiService()
