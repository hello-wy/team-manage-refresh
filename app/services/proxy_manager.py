"""Managed login exits and credential-free proxy diagnostics."""
import asyncio
import ipaddress
import time
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession as CurlSession
from sqlalchemy import func, select
from sqlalchemy.exc import InvalidRequestError

from app.models import AccountPoolEntry, AccountProxyRoute, MemberAuthorization, ProxyEndpoint, Team, TeamEmailMapping
from app.services.encryption import encryption_service
from app.services.settings import settings_service
from app.utils.proxy import build_curl_cffi_proxies, mask_proxy_url, normalize_proxy_url
from app.utils.time_utils import get_now

IP_CHECK_URL = "https://api.ipify.org?format=json"
GEO_CHECK_URL = "https://ipwho.is/"
DEFAULT_ROUTE = "*"


class ProxyRoutingError(ValueError):
    pass


def validate_managed_proxy(value):
    value = normalize_proxy_url(value)
    if not value or len(value) > 4096:
        raise ValueError("请填写有效代理地址")
    parsed = urlparse(value)
    if (not parsed.port or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)):
        raise ValueError("代理地址需要主机和端口，不能包含路径、查询参数或空白")
    return value


async def public_proxy(db, proxy):
    bound = await db.scalar(select(func.count()).select_from(AccountProxyRoute).where(
        AccountProxyRoute.proxy_id == proxy.id, AccountProxyRoute.email != DEFAULT_ROUTE,
    ))
    try:
        address = mask_proxy_url(encryption_service.decrypt_token(proxy.url_encrypted))
    except Exception:
        address = "配置无法解密，请重新编辑地址"
    return {
        "id": proxy.id, "name": proxy.name,
        "address": address,
        "enabled": proxy.enabled, "test_status": proxy.test_status,
        "exit_ip": proxy.exit_ip, "country": proxy.country, "region": proxy.region,
        "city": proxy.city, "latency_ms": proxy.latency_ms,
        "test_message": proxy.test_message,
        "tested_at": proxy.tested_at.isoformat() if proxy.tested_at else None,
        "account_count": int(bound or 0),
    }


async def known_accounts(db):
    """Return all configurable identities, including owners outside the pool."""
    accounts = {}
    for email, state in (await db.execute(select(
        AccountPoolEntry.email, AccountPoolEntry.liveness_status,
    ).where(AccountPoolEntry.deleted_at.is_(None)))).all():
        accounts[email.strip().lower()] = {"email": email.strip().lower(), "status": state, "source": "号池"}
    for email in (await db.scalars(select(Team.email).distinct())).all():
        email = email.strip().lower()
        accounts.setdefault(email, {"email": email, "status": None, "source": "Team 所有者"})
        accounts[email]["source"] = "Team 所有者"
    for email in (await db.scalars(select(MemberAuthorization.email).distinct())).all():
        email = email.strip().lower()
        accounts.setdefault(email, {"email": email, "status": None, "source": "Team 成员"})
    routes = {r.email: r for r in (await db.scalars(select(AccountProxyRoute))).all()}
    for email, account in accounts.items():
        route = routes.get(email)
        account.update(mode=route.mode if route else "inherit", proxy_id=route.proxy_id if route else None)
    return sorted(accounts.values(), key=lambda item: item["email"])


async def save_route(db, email, mode, proxy_id):
    email = email.strip().lower()
    if email != DEFAULT_ROUTE and email not in {r["email"] for r in await known_accounts(db)}:
        raise ValueError("账号不存在，请先将账号加入号池或 Team")
    if mode == "proxy":
        proxy = await db.get(ProxyEndpoint, proxy_id) if proxy_id else None
        if proxy is None or not proxy.enabled:
            raise ValueError("请选择已启用的代理")
    elif proxy_id is not None:
        raise ValueError("只有指定代理模式可以设置代理节点")
    route = await db.get(AccountProxyRoute, email)
    if mode == "inherit":
        if route:
            await db.delete(route)
    else:
        if route is None:
            route = AccountProxyRoute(email=email)
            db.add(route)
        route.mode, route.proxy_id = mode, proxy_id
    await db.commit()


async def identifier_email(db, identifier):
    """Resolve legacy identity keys used by OAuth, liveness and refresh flows."""
    if identifier.startswith("team-import-login-"):
        return identifier[len("team-import-login-"):].strip().lower()
    if "@" in identifier:
        return identifier.strip().lower()
    for prefix, model in (
        ("account-pool-login-", AccountPoolEntry),
        ("account-pool-liveness-", AccountPoolEntry),
        ("team-refresh-login-", Team),
        ("member-auto-login-", MemberAuthorization),
        ("member-oauth-", MemberAuthorization),
        ("member-delete-", MemberAuthorization),
        ("member-departure-", TeamEmailMapping),
    ):
        if identifier.startswith(prefix):
            value = identifier[len(prefix):]
            record = await db.get(model, int(value)) if value.isdigit() else None
            if record is None:
                raise ProxyRoutingError("无法确认账号登录出口，账号记录不存在")
            return record.email.strip().lower()
    if identifier.startswith("acc_"):
        return await db.scalar(select(Team.email).where(
            Team.account_id == identifier[4:],
        ).order_by(Team.id).limit(1))
    return None


async def resolve_login_proxy(db, identifier, *, email=None):
    if db is None:
        raise ProxyRoutingError("缺少数据库会话，无法确认账号出口")
    email = (email or await identifier_email(db, identifier) or "").strip().lower()
    route = await db.get(AccountProxyRoute, email) if email else None
    route = route or await db.get(AccountProxyRoute, DEFAULT_ROUTE)
    if route:
        if route.mode == "direct":
            return None
        proxy = await db.get(ProxyEndpoint, route.proxy_id)
        if proxy is None or not proxy.enabled:
            raise ProxyRoutingError("账号指定的代理已停用或不存在，请先在代理管理中调整出口")
        try:
            return validate_managed_proxy(encryption_service.decrypt_token(proxy.url_encrypted))
        except Exception as exc:
            raise ProxyRoutingError("账号代理配置无法读取，请在代理管理中重新保存") from exc
    config = await settings_service.get_proxy_config(db)
    if config["enabled"]:
        if not config["proxy"]:
            raise ProxyRoutingError("系统代理已启用但地址为空，请先配置代理")
        return normalize_proxy_url(config["proxy"])
    return None


class ProxyProbe:
    """Test fixed public endpoints only; never retry via the server's direct IP."""
    def __init__(self, session_factory=CurlSession):
        self.session_factory = session_factory
        self._semaphore = asyncio.Semaphore(4)
        self._running = set()

    async def test(self, db, proxy_id):
        if proxy_id in self._running:
            raise ValueError("该代理正在检测，请等待结果")
        self._running.add(proxy_id)
        try:
            async with self._semaphore:
                return await self._test(db, proxy_id)
        finally:
            self._running.discard(proxy_id)

    async def _test(self, db, proxy_id):
        proxy = await db.get(ProxyEndpoint, proxy_id)
        if proxy is None:
            raise ValueError("代理不存在")
        version = proxy.url_encrypted
        result = {"test_status": "offline", "exit_ip": None, "country": None,
                  "region": None, "city": None, "latency_ms": None,
                  "test_message": "连接失败，请检查地址、认证和代理服务状态"}
        started = time.monotonic()
        try:
            url = validate_managed_proxy(encryption_service.decrypt_token(version))
            async with self.session_factory(
                proxies=build_curl_cffi_proxies(url), impersonate="chrome110",
                timeout=12, verify=True, trust_env=False,
            ) as client:
                response = await client.get(IP_CHECK_URL, allow_redirects=False)
                result["latency_ms"] = max(1, round((time.monotonic() - started) * 1000))
                if response.status_code != 200:
                    result.update(test_status="error", test_message=f"出口检测服务返回 HTTP {response.status_code}")
                else:
                    ip = str(ipaddress.ip_address(response.json()["ip"]))
                    result.update(test_status="online", exit_ip=ip, test_message="代理连接正常")
                    try:
                        # Geography is looked up for the observed IP. A rotating proxy
                        # must not make a second request's exit look like the first one.
                        geo = await client.get(GEO_CHECK_URL + ip, allow_redirects=False)
                        data = geo.json() if geo.status_code == 200 else {}
                        if data.get("success") is not True:
                            raise ValueError("geo_unavailable")
                        for key in ("country", "region", "city"):
                            result[key] = str(data.get(key) or "")[:100] or None
                    except Exception:
                        result["test_message"] = "代理可用，地理信息暂未取得，可稍后重新检测"
        except (ValueError, KeyError, TypeError):
            result.update(test_status="error", test_message="出口检测响应无效或代理配置不可用")
        except Exception:
            # Do not persist raw exceptions: curl messages can contain credentials.
            pass
        if result["latency_ms"] is None:
            result["latency_ms"] = max(1, round((time.monotonic() - started) * 1000))
        result["tested_at"] = get_now()
        try:
            await db.refresh(proxy)
        except InvalidRequestError as exc:
            raise ValueError("检测期间代理已被删除") from exc
        if proxy.url_encrypted != version:
            raise ValueError("检测期间代理地址已变化，请重新检测")
        for key, value in result.items():
            setattr(proxy, key, value)
        await db.commit()
        return await public_proxy(db, proxy)


proxy_probe = ProxyProbe()
