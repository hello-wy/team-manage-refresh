"""HTTP adapter for API Key exports; credentials are never returned to the page."""
import httpx

from app.services.official_key_payload import GROUP_ID, TEMPLATE_NAME, build_payload, only_match
from app.services.sub2api import (
    REQUEST_TIMEOUT_SECONDS, Sub2apiError, Sub2apiImportUncertain, response_data,
)

ACCOUNT_PAGE_SIZE = 100


class OfficialKeySub2api:
    def __init__(self, sub2api):
        self._sub2api = sub2api

    async def config(self, db):
        return await self._sub2api._config(db)

    async def _read(self, config, *, path, params=None):
        try:
            async with self._sub2api.client_factory(
                timeout=REQUEST_TIMEOUT_SECONDS, headers={"x-api-key": config.api_key}
            ) as client:
                response = await client.get(f"{config.base_url}/api/v1/admin/{path}", params=params)
        except httpx.RequestError as exc:
            raise Sub2apiError("无法读取 Sub2API 账号配置，请检查网络") from exc
        return response_data(response)

    async def _template(self, config):
        page = 1
        matches = []
        while True:
            data = await self._read(config, path="accounts", params={
                "platform": "openai", "type": "apikey", "search": TEMPLATE_NAME,
                "page": page, "page_size": ACCOUNT_PAGE_SIZE,
            })
            if (not isinstance(data, dict) or not isinstance(data.get("items"), list)
                    or type(data.get("pages")) is not int):
                raise Sub2apiError("Sub2API 账号列表格式错误")
            matches.extend(data["items"])
            if page >= data["pages"]:
                break
            page += 1
        template = only_match(matches, name=TEMPLATE_NAME, kind="模板账号")
        return await self._read(config, path=f"accounts/{template['id']}")

    async def _group(self, config):
        try:
            group = await self._read(config, path=f"groups/{GROUP_ID}")
        except Sub2apiError as exc:
            raise Sub2apiError(f"读取 Sub2API 官key分组（ID {GROUP_ID}）失败：{exc}") from exc
        if not isinstance(group, dict) or type(group.get("id")) is not int or group["id"] != GROUP_ID:
            raise Sub2apiError(f"Sub2API 官key分组（ID {GROUP_ID}）响应格式或 ID 错误")
        if group.get("platform") != "openai":
            raise Sub2apiError(f"Sub2API 官key分组（ID {GROUP_ID}）必须是 OpenAI 分组")
        if group.get("status") != "active":
            raise Sub2apiError(f"Sub2API 官key分组（ID {GROUP_ID}）未启用")
        return group

    async def payload(self, db, *, config, entry):
        group = await self._group(config)
        template = await self._template(config)
        return build_payload(template, entry=entry, group_id=group["id"]), template["id"]

    async def verify_target(self, config, *, account_id, template_id):
        if account_id == template_id:
            raise Sub2apiError("导出关联指向模板账号，未修改模板")
        target = await self._read(config, path=f"accounts/{account_id}")
        if (not isinstance(target, dict) or target.get("id") != account_id
                or target.get("platform") != "openai" or target.get("type") != "apikey"):
            raise Sub2apiError("已导出的 Sub2API 账号类型发生变化，未更新")

    async def write(self, config, *, payload, record):
        account_id = record.account_id
        path = f"accounts/{account_id}" if account_id else "accounts"
        method = "PUT" if account_id else "POST"
        try:
            async with self._sub2api.client_factory(
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"x-api-key": config.api_key, "Idempotency-Key": record.operation_id},
            ) as client:
                response = await client.request(
                    method, f"{config.base_url}/api/v1/admin/{path}", json=payload
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise Sub2apiError("无法连接 Sub2API，未完成导出") from exc
        except httpx.RequestError as exc:
            raise Sub2apiImportUncertain("Sub2API 导出结果不确定，请到目标站点核对") from exc
        try:
            created = response_data(response)
        except Sub2apiError as exc:
            if response.is_success or response.status_code >= 500:
                raise Sub2apiImportUncertain("Sub2API 导出结果不确定，请到目标站点核对") from exc
            raise
        if (not isinstance(created, dict) or type(created.get("id")) is not int
                or created["id"] <= 0):
            raise Sub2apiImportUncertain("Sub2API 响应缺少账号 ID，请到目标站点核对")
        if account_id and created["id"] != account_id:
            raise Sub2apiImportUncertain("Sub2API 返回的账号 ID 不一致，请到目标站点核对")
        return created["id"]
