"""Read all selectable workspaces after credential verification."""
import secrets
from urllib.parse import parse_qs, urlparse

from app.services.openai_auth_protocol import auth_headers, inspect_auth_page_workspace, is_callback
from app.services.openai_auth_session import auth_session_claims
from app.services.openai_automatic_login_models import OpenAIAutomaticLoginError
from app.services.openai_workspace import inspect_workspace_claims, merge_workspace_scans


async def discover_workspaces(session, current_url, *, device_id, sentinel_token, expected_state):
    if not is_callback(current_url) and not current_url.rstrip("/").endswith(("/consent", "/workspace")):
        raise OpenAIAutomaticLoginError("登录后未到达授权步骤，无法确认账号验活")
    cookie_scan = inspect_workspace_claims(auth_session_claims(session))
    if is_callback(current_url):
        state = (parse_qs(urlparse(current_url).query).get("state") or [""])[0]
        if not secrets.compare_digest(state, expected_state):
            raise OpenAIAutomaticLoginError("账号验活 OAuth state 不匹配")
        return cookie_scan
    response = await session.get(
        current_url, headers=auth_headers(device_id, current_url, sentinel_token=sentinel_token),
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise OpenAIAutomaticLoginError(f"读取 Team 选择页面失败（HTTP {response.status_code}）")
    page_scan = inspect_auth_page_workspace(str(getattr(response, "text", "") or ""))
    if not page_scan["available_workspaces"]:
        raise OpenAIAutomaticLoginError("授权页面未返回可识别的 Team 列表，无法确认已加入的 Team")
    return merge_workspace_scans([cookie_scan, page_scan])
