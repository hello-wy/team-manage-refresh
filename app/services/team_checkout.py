"""支付项目的服务端 Team 充值客户端。"""
from urllib.parse import urlsplit

import httpx

CHECKOUT_TIMEOUT_SECONDS = 60
STRIPE_CHECKOUT_HOST = "checkout.stripe.com"


class TeamCheckoutError(Exception):
    def __init__(self, message, *, status_code=424):
        super().__init__(message)
        self.status_code = status_code


def validate_checkout_url(value):
    if not isinstance(value, str):
        raise TeamCheckoutError("支付项目未返回 Stripe 支付链接")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != STRIPE_CHECKOUT_HOST
            or parsed.username or parsed.password or parsed.port not in {None, 443}
            or any(char.isspace() for char in value)):
        raise TeamCheckoutError("支付项目返回的 Stripe 支付链接无效")
    return value


def payment_response(response):
    try:
        result = response.json()
    except ValueError as exc:
        raise TeamCheckoutError(f"支付项目返回非 JSON 响应（HTTP {response.status_code}）") from exc
    if not isinstance(result, dict):
        raise TeamCheckoutError("支付项目响应格式错误")
    if not response.is_success:
        detail = result.get("detail")
        if isinstance(detail, list):
            detail = "；".join(item["msg"] for item in detail if isinstance(item, dict) and isinstance(item.get("msg"), str))
        message = detail if isinstance(detail, str) and detail else f"HTTP {response.status_code}"
        raise TeamCheckoutError(f"生成 Team 支付链接失败：{message}")
    return {"url": validate_checkout_url(result.get("url"))}


class TeamCheckoutClient:
    def __init__(self, config, client_factory):
        self._config = config
        self._client_factory = client_factory

    def configuration(self):
        key = self._config.team_checkout_api_key.get_secret_value()
        if not key:
            raise TeamCheckoutError("请先配置 Team 充值联动密钥", status_code=503)
        base_url = self._config.team_checkout_base_url.rstrip("/")
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path):
            raise TeamCheckoutError("Team 充值服务地址必须为 HTTPS 站点根地址", status_code=503)
        return base_url, key

    async def create(self, account_json, coupon):
        base_url, key = self.configuration()
        try:
            async with self._client_factory(timeout=CHECKOUT_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{base_url}/api/integrations/team/checkout",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"account_json": account_json, "coupon": coupon},
                )
        except httpx.RequestError as exc:
            raise TeamCheckoutError("无法连接支付项目，请检查服务网络及日志") from exc
        return payment_response(response)
