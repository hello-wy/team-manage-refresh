"""OpenAI OAuth automatic login using email, password and TOTP."""
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urljoin, urlparse

from app.services.openai_auth_errors import auth_failure_message
from app.services.openai_auth_session import auth_session_claims
from app.services.openai_sentinel import OpenAISentinelError, issue_sentinel_token
from app.utils.totp import TotpError, generate_totp

AUTH_ORIGIN = "https://auth.openai.com"
CALLBACK_HOST = "localhost:1455"
MAX_REDIRECTS = 20
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

logger = logging.getLogger(__name__)


class OpenAIAutomaticLoginError(ValueError):
    pass


@dataclass(frozen=True)
class AutomaticLoginDependencies:
    get_session: Callable[..., Awaitable[Any]]
    clear_session: Callable[[str], Awaitable[None]]
    exchange_code: Callable[..., Awaitable[dict[str, Any]]]
    issue_sentinel: Callable[[Any, str], Awaitable[str]] = issue_sentinel_token


@dataclass(frozen=True)
class AutomaticLoginRequest:
    email: str
    password: str
    totp_secret: str
    account_id: str
    oauth_draft: dict[str, str]
    db_session: Any
    identifier: str


def _json_body(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _next_url(response: Any) -> str:
    payload = _json_body(response)
    value = payload.get("continue_url") or payload.get("url")
    value = value or response.headers.get("location") or response.headers.get("Location")
    return urljoin(AUTH_ORIGIN, str(value)) if value else ""


def _is_callback(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    query = parse_qs(parsed.query)
    return parsed.netloc == CALLBACK_HOST and bool(query.get("code"))


def _auth_headers(
    device_id: str, referer: str, *, json_request: bool = False, sentinel_token: str = ""
) -> dict[str, str]:
    headers = {
        "Accept": "application/json" if json_request else "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": AUTH_ORIGIN,
        "Referer": referer or AUTH_ORIGIN,
        "oai-device-id": device_id,
    }
    if json_request:
        headers["Content-Type"] = "application/json"
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    return headers


def _mfa_factor_id(payload: dict[str, Any]) -> str:
    session = payload.get("oai-client-auth-session")
    if not isinstance(session, dict):
        session = {}
    factors: list[dict[str, Any]] = []
    for key in ("mfa_challenge_factors", "mfa_factors"):
        values = session.get(key)
        if isinstance(values, list):
            factors.extend(item for item in values if isinstance(item, dict))
    for factor in factors:
        if str(factor.get("factor_type") or "").lower() == "totp":
            return str(factor.get("id") or "").strip()
    return ""


def _is_mfa_challenge(payload: dict[str, Any], next_url: str) -> bool:
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    return str(page.get("type") or "").lower() == "mfa_challenge" or "/mfa-challenge/" in next_url.lower()


class OpenAIAutomaticLoginService:
    def __init__(self, dependencies: AutomaticLoginDependencies):
        self._dependencies = dependencies

    async def login(self, request: AutomaticLoginRequest) -> dict[str, Any]:
        await self._dependencies.clear_session(request.identifier)
        session = await self._dependencies.get_session(request.db_session, request.identifier)
        device_id = secrets.token_hex(16)
        self._set_device_cookie(session, device_id)
        sentinel_token = await self._sentinel(session, device_id)
        _, current_url = await self._follow(
            session, request.oauth_draft["authorize_url"], device_id, sentinel_token
        )
        current_url = await self._submit_email(
            session, request.email, current_url, device_id, sentinel_token
        )
        current_url = await self._submit_password_and_totp(
            session, request, current_url, device_id, sentinel_token
        )
        current_url = await self._select_workspace(
            session, current_url, request.account_id, device_id, sentinel_token
        )
        if not _is_callback(current_url):
            raise OpenAIAutomaticLoginError("自动登录未取得 OAuth 回调，请检查账号登录状态")
        logger.info("成员自动登录完成: identifier=%s", request.identifier)
        return await self._exchange(request, current_url)

    async def _sentinel(self, session: Any, device_id: str) -> str:
        try:
            return await self._dependencies.issue_sentinel(session, device_id)
        except OpenAISentinelError as exc:
            raise OpenAIAutomaticLoginError(
                f"自动登录 Sentinel 初始化失败（{exc}）"
            ) from exc

    @staticmethod
    def _set_device_cookie(session: Any, device_id: str) -> None:
        try:
            session.cookies.set("oai-did", device_id, domain="auth.openai.com", path="/")
        except Exception as exc:
            raise OpenAIAutomaticLoginError("无法初始化自动登录会话") from exc

    async def _follow(
        self, session: Any, start_url: str, device_id: str, sentinel_token: str
    ) -> tuple[Any, str]:
        current_url = urljoin(AUTH_ORIGIN, str(start_url or ""))
        response = None
        for _ in range(MAX_REDIRECTS):
            if _is_callback(current_url):
                return response, current_url
            response = await session.get(
                current_url,
                headers=_auth_headers(device_id, current_url, sentinel_token=sentinel_token),
                allow_redirects=False,
            )
            if response.status_code not in REDIRECT_STATUSES:
                return response, current_url
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                raise OpenAIAutomaticLoginError("自动登录重定向缺少目标地址")
            current_url = urljoin(current_url, location)
        raise OpenAIAutomaticLoginError("自动登录重定向次数过多")

    async def _submit_email(
        self, session: Any, email: str, current_url: str,
        device_id: str, sentinel_token: str,
    ) -> str:
        response = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/authorize/continue",
            headers=_auth_headers(
                device_id, current_url, json_request=True, sentinel_token=sentinel_token
            ),
            json={"username": {"value": email, "kind": "email"}},
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise OpenAIAutomaticLoginError(
                auth_failure_message("authorize_continue", response)
            )
        next_url = _next_url(response)
        if not next_url:
            raise OpenAIAutomaticLoginError("账号提交后未返回登录步骤")
        _, current_url = await self._follow(session, next_url, device_id, sentinel_token)
        if "password" not in current_url.lower():
            raise OpenAIAutomaticLoginError("该账号未进入密码登录流程，无法执行自动登录")
        return current_url

    async def _submit_password_and_totp(
        self, session: Any, request: AutomaticLoginRequest, current_url: str,
        device_id: str, sentinel_token: str,
    ) -> str:
        response = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/password/verify",
            headers=_auth_headers(
                device_id, current_url, json_request=True, sentinel_token=sentinel_token
            ),
            json={"password": request.password},
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise OpenAIAutomaticLoginError(auth_failure_message(
                "password_verify", response, identifier=request.identifier
            ))
        payload = _json_body(response)
        next_url = _next_url(response)
        if _is_mfa_challenge(payload, next_url):
            next_url = await self._complete_totp(
                session, request.totp_secret, payload, next_url, device_id, sentinel_token
            )
        if not next_url:
            raise OpenAIAutomaticLoginError("密码验证后未返回下一步")
        _, current_url = await self._follow(session, next_url, device_id, sentinel_token)
        if "email-verification" in current_url.lower():
            raise OpenAIAutomaticLoginError("该账号还要求邮箱验证码，当前自动登录仅支持密码和 2FA")
        return current_url

    async def _complete_totp(
        self, session: Any, secret: str, payload: dict[str, Any], referer: str,
        device_id: str, sentinel_token: str,
    ) -> str:
        factor_id = _mfa_factor_id(payload)
        if not factor_id:
            factor_id = _mfa_factor_id({
                "oai-client-auth-session": auth_session_claims(session)
            })
        if not factor_id:
            raise OpenAIAutomaticLoginError("2FA 挑战未返回 TOTP 因子")
        try:
            code = generate_totp(secret)
        except TotpError as exc:
            raise OpenAIAutomaticLoginError(str(exc)) from exc
        headers = _auth_headers(
            device_id, referer, json_request=True, sentinel_token=sentinel_token
        )
        issue = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/mfa/issue_challenge",
            headers=headers,
            json={"type": "totp", "id": factor_id, "force_fresh_challenge": False},
            allow_redirects=False,
        )
        if issue.status_code not in {200, 201, 202, 204}:
            raise OpenAIAutomaticLoginError(auth_failure_message("mfa_issue", issue))
        verify = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/mfa/verify",
            headers=headers,
            json={"type": "totp", "id": factor_id, "code": code},
            allow_redirects=False,
        )
        if verify.status_code != 200:
            raise OpenAIAutomaticLoginError(auth_failure_message("mfa_verify", verify))
        return _next_url(verify)

    async def _select_workspace(
        self, session: Any, current_url: str, account_id: str,
        device_id: str, sentinel_token: str,
    ) -> str:
        if _is_callback(current_url):
            return current_url
        if not current_url.rstrip("/").endswith(("/consent", "/workspace")):
            return current_url
        response = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/workspace/select",
            headers=_auth_headers(
                device_id, current_url, json_request=True, sentinel_token=sentinel_token
            ),
            json={"workspace_id": account_id},
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise OpenAIAutomaticLoginError(
                auth_failure_message("workspace_select", response)
            )
        _, selected_url = await self._follow(
            session, _next_url(response), device_id, sentinel_token
        )
        return selected_url

    async def _exchange(
        self, request: AutomaticLoginRequest, callback_url: str
    ) -> dict[str, Any]:
        query = parse_qs(urlparse(callback_url).query)
        state = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        if not secrets.compare_digest(state, request.oauth_draft["state"]):
            raise OpenAIAutomaticLoginError("自动登录 OAuth state 不匹配")
        result = await self._dependencies.exchange_code(
            code=code,
            client_id=request.oauth_draft["client_id"],
            redirect_uri=request.oauth_draft["redirect_uri"],
            code_verifier=request.oauth_draft["code_verifier"],
            db_session=request.db_session,
            identifier=request.identifier,
        )
        if not result.get("success"):
            raise OpenAIAutomaticLoginError("自动登录授权码兑换失败")
        return result
