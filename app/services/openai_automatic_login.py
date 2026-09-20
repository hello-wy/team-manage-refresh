"""OpenAI OAuth automatic login using email, password and TOTP."""
import base64
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

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


def _auth_headers(device_id: str, referer: str, *, json_request: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/json" if json_request else "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": AUTH_ORIGIN,
        "Referer": referer or AUTH_ORIGIN,
        "oai-device-id": device_id,
    }
    if json_request:
        headers["Content-Type"] = "application/json"
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


def _decode_jwt_segment(segment: str) -> dict[str, Any]:
    try:
        padded = segment + "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return {}


def _auth_session_claims(session: Any) -> dict[str, Any]:
    try:
        cookie = unquote(str(session.cookies.get("oai-client-auth-session") or ""))
    except Exception:
        return {}
    segments = cookie.split(".")
    return _decode_jwt_segment(segments[1]) if len(segments) > 1 else {}


def _workspace_ids(session: Any) -> set[str]:
    claims = _auth_session_claims(session)
    workspaces = claims.get("workspaces") if isinstance(claims, dict) else []
    if not isinstance(workspaces, list):
        return set()
    return {str(item.get("id") or "") for item in workspaces if isinstance(item, dict)} - {""}


class OpenAIAutomaticLoginService:
    def __init__(self, dependencies: AutomaticLoginDependencies):
        self._dependencies = dependencies

    async def login(self, request: AutomaticLoginRequest) -> dict[str, Any]:
        await self._dependencies.clear_session(request.identifier)
        session = await self._dependencies.get_session(request.db_session, request.identifier)
        device_id = secrets.token_hex(16)
        self._set_device_cookie(session, device_id)
        _, current_url = await self._follow(session, request.oauth_draft["authorize_url"], device_id)
        current_url = await self._submit_email(session, request.email, current_url, device_id)
        current_url = await self._submit_password_and_totp(
            session, request, current_url, device_id
        )
        current_url = await self._select_workspace(
            session, current_url, request.account_id, device_id
        )
        if not _is_callback(current_url):
            raise OpenAIAutomaticLoginError("自动登录未取得 OAuth 回调，请检查账号登录状态")
        logger.info("成员自动登录完成: identifier=%s", request.identifier)
        return await self._exchange(request, current_url)

    @staticmethod
    def _set_device_cookie(session: Any, device_id: str) -> None:
        try:
            session.cookies.set("oai-did", device_id, domain="auth.openai.com", path="/")
        except Exception as exc:
            raise OpenAIAutomaticLoginError("无法初始化自动登录会话") from exc

    async def _follow(self, session: Any, start_url: str, device_id: str) -> tuple[Any, str]:
        current_url = urljoin(AUTH_ORIGIN, str(start_url or ""))
        response = None
        for _ in range(MAX_REDIRECTS):
            if _is_callback(current_url):
                return response, current_url
            response = await session.get(
                current_url,
                headers=_auth_headers(device_id, current_url),
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
        self, session: Any, email: str, current_url: str, device_id: str
    ) -> str:
        response = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/authorize/continue",
            headers=_auth_headers(device_id, current_url, json_request=True),
            json={"username": {"value": email, "kind": "email"}},
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise OpenAIAutomaticLoginError(f"账号提交失败（HTTP {response.status_code}）")
        next_url = _next_url(response)
        if not next_url:
            raise OpenAIAutomaticLoginError("账号提交后未返回登录步骤")
        _, current_url = await self._follow(session, next_url, device_id)
        if "password" not in current_url.lower():
            raise OpenAIAutomaticLoginError("该账号未进入密码登录流程，无法执行自动登录")
        return current_url

    async def _submit_password_and_totp(
        self, session: Any, request: AutomaticLoginRequest, current_url: str, device_id: str
    ) -> str:
        response = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/password/verify",
            headers=_auth_headers(device_id, current_url, json_request=True),
            json={"password": request.password},
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise OpenAIAutomaticLoginError(f"密码验证失败（HTTP {response.status_code}）")
        payload = _json_body(response)
        next_url = _next_url(response)
        if _is_mfa_challenge(payload, next_url):
            next_url = await self._complete_totp(
                session, request.totp_secret, payload, next_url, device_id
            )
        if not next_url:
            raise OpenAIAutomaticLoginError("密码验证后未返回下一步")
        _, current_url = await self._follow(session, next_url, device_id)
        if "email-verification" in current_url.lower():
            raise OpenAIAutomaticLoginError("该账号还要求邮箱验证码，当前自动登录仅支持密码和 2FA")
        return current_url

    async def _complete_totp(
        self, session: Any, secret: str, payload: dict[str, Any], referer: str, device_id: str
    ) -> str:
        factor_id = _mfa_factor_id(payload)
        if not factor_id:
            factor_id = _mfa_factor_id({
                "oai-client-auth-session": _auth_session_claims(session)
            })
        if not factor_id:
            raise OpenAIAutomaticLoginError("2FA 挑战未返回 TOTP 因子")
        try:
            code = generate_totp(secret)
        except TotpError as exc:
            raise OpenAIAutomaticLoginError(str(exc)) from exc
        headers = _auth_headers(device_id, referer, json_request=True)
        issue = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/mfa/issue_challenge",
            headers=headers,
            json={"type": "totp", "id": factor_id, "force_fresh_challenge": False},
            allow_redirects=False,
        )
        if issue.status_code not in {200, 201, 202, 204}:
            raise OpenAIAutomaticLoginError(f"2FA 挑战创建失败（HTTP {issue.status_code}）")
        verify = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/mfa/verify",
            headers=headers,
            json={"type": "totp", "id": factor_id, "code": code},
            allow_redirects=False,
        )
        if verify.status_code != 200:
            raise OpenAIAutomaticLoginError(f"2FA 验证失败（HTTP {verify.status_code}）")
        return _next_url(verify)

    async def _select_workspace(
        self, session: Any, current_url: str, account_id: str, device_id: str
    ) -> str:
        if _is_callback(current_url):
            return current_url
        if not current_url.rstrip("/").endswith(("/consent", "/workspace")):
            return current_url
        workspace_ids = _workspace_ids(session)
        if account_id not in workspace_ids:
            raise OpenAIAutomaticLoginError("登录账号无权访问当前 Team 工作区")
        response = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/workspace/select",
            headers=_auth_headers(device_id, current_url, json_request=True),
            json={"workspace_id": account_id},
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise OpenAIAutomaticLoginError(f"Team 工作区选择失败（HTTP {response.status_code}）")
        _, selected_url = await self._follow(session, _next_url(response), device_id)
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
