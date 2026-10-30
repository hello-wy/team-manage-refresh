"""Query balances through OpenAI's Dashboard billing endpoint."""
import asyncio
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

import httpx


OPENAI_BASE_URL = "https://api.openai.com"
BILLING_CREDIT_GRANTS_PATH = "/dashboard/billing/credit_grants"
BALANCE_REQUEST_TIMEOUT_SECONDS = 20.0
BALANCE_FIELDS = ("total_granted", "total_used", "total_available")
KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_.*-]+")


class OfficialKeyBalanceError(RuntimeError):
    """Raised when OpenAI returns an unusable balance response."""


def _money_value(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if isinstance(value, bool) or value is None:
        raise OfficialKeyBalanceError(f"OpenAI 余额响应缺少 {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OfficialKeyBalanceError(f"OpenAI 余额响应中的 {field} 无效") from exc
    if not parsed.is_finite():
        raise OfficialKeyBalanceError(f"OpenAI 余额响应中的 {field} 无效")
    return format(parsed, "f")


def normalize_balance(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise OfficialKeyBalanceError("OpenAI 余额响应格式错误")
    return {
        field: _money_value(payload, field)
        for field in BALANCE_FIELDS
    } | {"currency": "USD"}


def _upstream_error(response: httpx.Response, *, api_key: str) -> str:
    prefix = f"OpenAI Dashboard 余额接口返回 HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return f"{prefix}（非 JSON 响应）"
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str) or not message.strip():
        return prefix
    redacted = KEY_PATTERN.sub("[已隐藏 Key]", message.replace(api_key, "[已隐藏 Key]"))
    return f"{prefix}：{redacted}"


class OfficialKeyBalanceService:
    def __init__(self, client_factory: Callable[..., Any] = httpx.AsyncClient):
        self._client_factory = client_factory

    async def query(self, api_key: str) -> dict[str, str]:
        try:
            async with self._client_factory(
                timeout=BALANCE_REQUEST_TIMEOUT_SECONDS,
                headers={"Authorization": f"Bearer {api_key}"},
            ) as client:
                response = await client.get(
                    f"{OPENAI_BASE_URL}{BILLING_CREDIT_GRANTS_PATH}"
                )
        except httpx.RequestError as exc:
            raise OfficialKeyBalanceError("无法连接 OpenAI Dashboard 余额接口，请检查网络") from exc
        if not response.is_success:
            raise OfficialKeyBalanceError(_upstream_error(response, api_key=api_key))
        try:
            payload = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise OfficialKeyBalanceError("OpenAI Dashboard 余额接口返回非 JSON 响应") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise OfficialKeyBalanceError(_upstream_error(response, api_key=api_key))
        return normalize_balance(payload)

    async def query_many(self, api_keys: Mapping[int, str]) -> dict[int, dict[str, Any]]:
        results = await asyncio.gather(*(
            self._query_one(entry_id, api_key)
            for entry_id, api_key in api_keys.items()
        ))
        return dict(results)

    async def _query_one(self, entry_id: int, api_key: str):
        try:
            return entry_id, {"status": "ok", "balance": await self.query(api_key)}
        except OfficialKeyBalanceError as exc:
            return entry_id, {"status": "error", "error": str(exc)}
