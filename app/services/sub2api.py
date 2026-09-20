"""Push an authorized member's sub2api account to the configured instance."""
import json
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.services.encryption import encryption_service
from app.services.settings import settings_service

DEFAULT_BASE_URL = "https://solidapi.top"
REQUEST_TIMEOUT_SECONDS = 20.0
GROUP_MODE_ALL = "all"
GROUP_MODE_SELECTED = "selected"


class Sub2apiError(ValueError):
    pass


@dataclass(frozen=True)
class Sub2apiConfig:
    base_url: str
    api_key: str


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

    async def _config(self, db, base_url=None, api_key=None):
        configured_url = base_url or await settings_service.get_setting(
            db, "sub2api_base_url", DEFAULT_BASE_URL)
        configured_key = (api_key or "").strip()
        if not configured_key:
            encrypted = await settings_service.get_setting(db, "sub2api_api_key_encrypted", "")
            if not encrypted:
                raise Sub2apiError("请先在系统中心设置 sub2api x-api-key")
            configured_key = encryption_service.decrypt_token(encrypted)
        return Sub2apiConfig(normalize_base_url(configured_url), configured_key)

    async def list_openai_groups(self, db, base_url=None, api_key=None):
        config = await self._config(db, base_url, api_key)
        try:
            async with self.client_factory(
                    timeout=REQUEST_TIMEOUT_SECONDS, headers={"x-api-key": config.api_key}) as client:
                response = await client.get(f"{config.base_url}/api/v1/admin/groups/all",
                                            params={"platform": "openai"})
        except httpx.RequestError as exc:
            raise Sub2apiError("无法连接 sub2api，请检查站点地址与网络") from exc
        groups = response_data(response)
        if not isinstance(groups, list):
            raise Sub2apiError("sub2api 分组响应格式错误")
        return [group for group in groups if isinstance(group, dict)
                and group.get("platform") == "openai"
                and isinstance(group.get("id"), int) and group["id"] > 0]

    async def _selected_group_ids(self, db, groups):
        available_ids = {group["id"] for group in groups}
        mode = await settings_service.get_setting(db, "sub2api_group_mode", GROUP_MODE_ALL)
        if mode != GROUP_MODE_SELECTED:
            return sorted(available_ids)
        raw_ids = await settings_service.get_setting(db, "sub2api_group_ids", "[]")
        try:
            selected_ids = {int(value) for value in json.loads(raw_ids)}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise Sub2apiError("sub2api 分组配置损坏，请在系统中心重新保存") from exc
        missing_ids = selected_ids - available_ids
        if missing_ids:
            missing = ", ".join(str(value) for value in sorted(missing_ids))
            raise Sub2apiError(f"已配置的 sub2api 分组不存在或已停用：{missing}，请重新获取并保存分组")
        if not selected_ids:
            raise Sub2apiError("尚未选择要导出的 sub2api 分组")
        return sorted(selected_ids)

    async def import_member(self, payload, db):
        config = await self._config(db)
        groups = await self.list_openai_groups(db, config.base_url, config.api_key)
        if not groups:
            raise Sub2apiError("sub2api 没有可用的 OpenAI 分组，未创建账户")
        group_ids = await self._selected_group_ids(db, groups)
        account = {**payload["accounts"][0], "platform": "openai", "group_ids": group_ids}
        try:
            async with self.client_factory(
                    timeout=REQUEST_TIMEOUT_SECONDS, headers={"x-api-key": config.api_key}) as client:
                response = await client.post(f"{config.base_url}/api/v1/admin/accounts", json=account)
        except httpx.RequestError as exc:
            raise Sub2apiError("无法连接 sub2api，请检查站点地址与网络") from exc
        created = response_data(response)
        if not isinstance(created, dict) or not isinstance(created.get("id"), int):
            raise Sub2apiError("sub2api 创建响应缺少账户 ID，请到目标站点核对是否已创建")
        return {"account_id": created["id"], "group_count": len(group_ids)}


sub2api_service = Sub2apiService()
