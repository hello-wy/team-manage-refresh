"""OpenAI OAuth automatic login using email, password and TOTP."""
import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from app.services.openai_auth_errors import auth_failure_message
from app.services.openai_auth_protocol import (
    AUTH_ORIGIN,
    REDIRECT_STATUSES,
    auth_headers as _auth_headers,
    inspect_auth_page_workspace,
    is_callback as _is_callback,
    is_mfa_challenge as _is_mfa_challenge,
    json_body as _json_body,
    next_url as _next_url,
)
from app.services.openai_auth_session import auth_session_claims
from app.services.openai_automatic_login_models import (
    AutomaticLoginDependencies,
    AutomaticLoginRequest,
    OpenAIAutomaticLoginError,
)
from app.services.openai_sentinel import OpenAISentinelError
from app.services.openai_workspace_discovery import discover_workspaces
from app.services.openai_workspace import inspect_workspace_claims, resolve_workspace_id, token_workspace_id
from app.services.openai_workspace import token_claims, token_plan_type
from app.services.openai_subscription import personal_subscription, query_personal_subscription
from app.services.openai_auth_totp import complete_totp
from app.services.operation_progress import report_progress
from app.services.proxy_manager import ProxyRoutingError
from app.services.openai_auth_email import (
    PASSWORD_URL, email_login_options, is_email_verification, is_password_login,
)

MAX_REDIRECTS = 20

logger = logging.getLogger(__name__)


class OpenAIAutomaticLoginService:
    def __init__(self, dependencies: AutomaticLoginDependencies):
        self._dependencies = dependencies

    async def login(self, request: AutomaticLoginRequest) -> dict[str, Any]:
        await self._dependencies.clear_session(request.identifier)
        session = await self._login_session(request)
        device_id = secrets.token_hex(16)
        self._set_device_cookie(session, device_id)
        sentinel_token = await self._sentinel(session, device_id)
        _, current_url = await self._follow(
            session, request.oauth_draft["authorize_url"], device_id, sentinel_token
        )
        current_url = await self._submit_email(
            session, request.email, current_url, device_id, sentinel_token
        )
        current_url, verified_totp_factor_id = await self._submit_password_and_totp(
            session, request, current_url, device_id, sentinel_token
        )
        phone_number = ""
        phone_rental_error = ""
        if "add-phone" in current_url.lower() or "add_phone" in current_url.lower():
            if not request.phone_verification:
                raise OpenAIAutomaticLoginError("该账号要求添加手机号，请从手机号接码页面发起")
            if self._dependencies.complete_phone_verification is None:
                raise OpenAIAutomaticLoginError("手机号接码服务未配置")
            phone_result = await self._dependencies.complete_phone_verification(
                request, session, device_id, sentinel_token, current_url
            )
            phone_number = str(phone_result["phone_number"])
            phone_rental_error = str(phone_result.get("rental_cleanup_error") or "")
            _, current_url = await self._follow(
                session, phone_result["continue_url"], device_id, sentinel_token
            )
        if phone_number:
            await report_progress(request.on_progress, "oauth")
        current_url, workspace = await self._select_workspace(
            session, current_url, request.account_id, device_id, sentinel_token
        )
        if not _is_callback(current_url):
            raise OpenAIAutomaticLoginError("自动登录未取得 OAuth 回调，请检查账号登录状态")
        logger.info("成员自动登录完成: identifier=%s", request.identifier)
        result = await self._exchange(request, current_url)
        actual_id = token_workspace_id(str(result.get("access_token") or ""))
        if actual_id and not workspace.get("workspace_id") and workspace.get("status") != "no_workspace":
            workspace["workspace_id"] = actual_id
            workspace["status"] = "workspace_ok"
        result["workspace"] = workspace
        if request.read_subscription:
            plan = await self._personal_subscription(session, result)
            if plan:
                result["subscription"] = {"personal_plan": plan}
        result["verified_totp_factor_id"] = verified_totp_factor_id
        result["phone_number"] = phone_number
        result["phone_rental_error"] = phone_rental_error
        return result

    async def verify_credentials(self, request: AutomaticLoginRequest) -> dict[str, Any]:
        """Verify credentials, optionally reading a personal subscription in this session."""
        await self._dependencies.clear_session(request.identifier)
        try:
            session = await self._login_session(request)
            device_id = secrets.token_hex(16)
            self._set_device_cookie(session, device_id)
            sentinel_token = await self._sentinel(session, device_id)
            _, current_url = await self._follow(
                session, request.oauth_draft["authorize_url"], device_id, sentinel_token
            )
            current_url = await self._submit_email(
                session, request.email, current_url, device_id, sentinel_token
            )
            current_url, _ = await self._submit_password_and_totp(
                session, request, current_url, device_id, sentinel_token
            )
            scan = await discover_workspaces(
                session, current_url, device_id=device_id, sentinel_token=sentinel_token,
                expected_state=request.oauth_draft["state"],
            )
            if request.read_subscription:
                await self._scan_personal_subscription(
                    request, session, scan, current_url, device_id, sentinel_token,
                )
            return scan
        finally:
            await self._dependencies.clear_session(request.identifier)

    async def _personal_subscription(self, session, result):
        token = str(result.get("access_token") or "")
        claims = token_claims(token)
        if not claims:
            return ""
        workspace = result.get("workspace") or {}
        token_id = token_workspace_id(token)
        if workspace.get("workspace_id") and token_id and workspace["workspace_id"] != token_id:
            return ""
        primary_plan = token_plan_type(claims)
        plan = personal_subscription(claims)
        # A Team workspace's usage must not be presented as a personal plan.
        if primary_plan and primary_plan != "personal" and not personal_subscription({"plan_type": primary_plan}):
            return ""
        identity_token = str(result.get("id_token") or "")
        identity_id = token_workspace_id(identity_token)
        identity_plan = personal_subscription(token_claims(identity_token)) if (
            not identity_id or not token_id or identity_id == token_id
        ) else ""
        if not (plan or primary_plan == "personal" or workspace.get("is_personal")
                or workspace.get("status") == "personal_account" or identity_plan):
            return ""
        return await query_personal_subscription(session, token, token_id) or plan or identity_plan

    async def _scan_personal_subscription(self, request, session, scan, current_url, device_id, sentinel_token):
        personal = [item for item in scan.get("available_workspaces", [])
                    if isinstance(item, dict) and item.get("is_personal")]
        if len(personal) != 1 and not _is_callback(current_url):
            return
        try:
            callback_url = current_url
            workspace = {}
            if not _is_callback(callback_url):
                callback_url, workspace = await self._select_workspace(
                    session, current_url, personal[0]["id"], device_id, sentinel_token,
                )
                workspace = {**workspace, "is_personal": True, "status": "personal_account"}
            if not _is_callback(callback_url):
                return
            result = await self._exchange(request, callback_url)
            token_id = token_workspace_id(str(result.get("access_token") or ""))
            if not workspace:
                selected = next((item for item in personal if item.get("id") == token_id), {})
                workspace = {**selected, "workspace_id": token_id}
            result["workspace"] = workspace
            plan = await self._personal_subscription(session, result)
            if plan:
                scan["personal_plan"] = plan
                for item in personal:
                    if item.get("id") == token_id:
                        item["personal_plan"] = plan
        except Exception:
            # Subscription lookup is optional; successful credential verification stays valid.
            logger.info("验活通过，个人订阅获取未完成: identifier=%s", request.identifier)

    async def _login_session(self, request):
        try:
            return await self._dependencies.get_session(
                request.db_session, request.identifier, email=request.email,
            )
        except ProxyRoutingError as exc:
            raise OpenAIAutomaticLoginError(str(exc)) from exc

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
        page, current_url = await self._follow(session, next_url, device_id, sentinel_token)
        if is_email_verification(current_url):
            current_url = await self._choose_password_login(
                session, page, device_id, sentinel_token, stage="after_email",
            )
        if not is_password_login(current_url):
            raise OpenAIAutomaticLoginError("该账号未进入密码登录流程，无法执行自动登录")
        return current_url

    async def _choose_password_login(
        self, session, response, device_id, sentinel_token, *, stage, identifier="unknown",
        allow_switch=True,
    ):
        if getattr(response, "status_code", None) != 200:
            raise OpenAIAutomaticLoginError("读取邮箱验证页面失败，无法确认可用登录方式")
        mode, password_available = email_login_options(session, response)
        logger.info(
            "OpenAI 邮箱验证步骤: stage=%s mode=%s password_available=%s identifier=%s",
            stage, mode, password_available, identifier,
        )
        if mode == "post_auth_email_verification":
            raise OpenAIAutomaticLoginError(
                "OpenAI 在密码/2FA 步骤后仍要求验证邮箱（post_auth_email_verification），"
                "OAuth 授权尚未完成；请先在官方登录流程完成邮箱验证后重试"
            )
        if not password_available:
            raise OpenAIAutomaticLoginError(
                "OpenAI 当前要求邮箱验证，未提供可切换的密码登录入口；请在官方登录流程完成验证后重试"
            )
        if not allow_switch:
            raise OpenAIAutomaticLoginError(
                "已切换密码登录，但 OpenAI 仍返回邮箱验证，自动登录已停止；请在官方登录流程确认验证要求"
            )
        page, url = await self._follow(session, PASSWORD_URL, device_id, sentinel_token)
        if (not is_password_login(url) or getattr(page, "status_code", None) != 200):
            raise OpenAIAutomaticLoginError(
                "OpenAI 未允许切换到密码登录，仍需在官方登录流程完成验证"
            )
        return url

    async def _submit_password_and_totp(
        self, session: Any, request: AutomaticLoginRequest, current_url: str,
        device_id: str, sentinel_token: str,
    ) -> tuple[str, str]:
        # At most one advertised password fallback; never loop on an OTP challenge.
        for attempt in range(2):
            response, current_url, factor_id = await self._verify_password_and_totp_once(
                session, request, current_url, device_id, sentinel_token,
            )
            if not is_email_verification(current_url):
                return current_url, factor_id
            current_url = await self._choose_password_login(
                session, response, device_id, sentinel_token, stage="after_password",
                identifier=request.identifier, allow_switch=attempt == 0,
            )
        raise OpenAIAutomaticLoginError("密码登录未完成")

    async def _verify_password_and_totp_once(
        self, session: Any, request: AutomaticLoginRequest, current_url: str,
        device_id: str, sentinel_token: str,
    ) -> tuple[Any, str, str]:
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
        verified_totp_factor_id = ""
        if _is_mfa_challenge(payload, next_url):
            next_url, verified_totp_factor_id = await complete_totp(
                session, request.totp_secret, payload=payload, referer=next_url,
                device_id=device_id, sentinel_token=sentinel_token,
            )
        if not next_url:
            raise OpenAIAutomaticLoginError("密码验证后未返回下一步")
        page, current_url = await self._follow(session, next_url, device_id, sentinel_token)
        return page, current_url, verified_totp_factor_id

    async def _select_workspace(
        self, session: Any, current_url: str, account_id: str,
        device_id: str, sentinel_token: str,
    ) -> tuple[str, dict[str, Any]]:
        workspace = inspect_workspace_claims(auth_session_claims(session))
        if _is_callback(current_url):
            return current_url, workspace
        if not current_url.rstrip("/").endswith(("/consent", "/workspace")):
            return current_url, workspace
        selected_id = resolve_workspace_id(workspace, account_id)
        if not selected_id:
            workspace = await self._workspace_from_page(
                session, current_url, device_id, sentinel_token
            )
            selected_id = resolve_workspace_id(workspace, account_id)
        if not selected_id:
            raise OpenAIAutomaticLoginError(
                "登录后存在多个 workspace 且无法确定当前项，请先获取当前 Team"
                if workspace.get("available_workspaces")
                else "登录后未返回可选择的 workspace"
            )
        response = await session.post(
            f"{AUTH_ORIGIN}/api/accounts/workspace/select",
            headers=_auth_headers(
                device_id, current_url, json_request=True, sentinel_token=sentinel_token
            ),
            json={"workspace_id": selected_id},
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise OpenAIAutomaticLoginError(
                auth_failure_message("workspace_select", response)
            )
        _, selected_url = await self._follow(
            session, _next_url(response), device_id, sentinel_token
        )
        selected = next(
            (
                item for item in workspace.get("available_workspaces", [])
                if item.get("id") == selected_id
            ),
            {},
        )
        workspace.update({
            "status": "personal_account" if selected.get("is_personal") else "workspace_ok",
            "workspace_id": selected_id,
            "workspace_name": str(selected.get("name") or ""),
            "is_personal": bool(selected.get("is_personal")),
        })
        return selected_url, workspace

    async def _workspace_from_page(
        self, session: Any, current_url: str, device_id: str, sentinel_token: str
    ) -> dict[str, Any]:
        response = await session.get(
            current_url,
            headers=_auth_headers(device_id, current_url, sentinel_token=sentinel_token),
            allow_redirects=False,
        )
        if response.status_code != 200:
            return inspect_workspace_claims({})
        return inspect_auth_page_workspace(str(getattr(response, "text", "") or ""))

    async def _exchange(
        self, request: AutomaticLoginRequest, callback_url: str
    ) -> dict[str, Any]:
        callback = urlparse(callback_url)
        expected = urlparse(request.oauth_draft["redirect_uri"])
        if (callback.scheme, callback.netloc, callback.path) != (expected.scheme, expected.netloc, expected.path):
            raise OpenAIAutomaticLoginError("自动登录 OAuth 回调地址不匹配")
        query = parse_qs(callback.query)
        if query.get("error") or len(query.get("code", [])) != 1 or len(query.get("state", [])) != 1:
            raise OpenAIAutomaticLoginError("自动登录未返回唯一的 OAuth 授权码和 state")
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
