"""Safe upstream error diagnostics for OpenAI authentication calls."""
import logging
import re
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SAFE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
STAGE_LABELS = {
    "authorize_continue": "账号提交失败",
    "password_verify": "密码验证失败",
    "mfa_issue": "2FA 挑战创建失败",
    "mfa_verify": "2FA 验证失败",
    "workspace_select": "Team 工作区选择失败",
}
CODE_MESSAGES = {
    "invalid_username_or_password": "OpenAI 拒绝了账号或密码，请更新账号号池中的登录密码后重试",
}


def _json_body(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_value(value: Any) -> str:
    text = str(value or "").strip()
    return text if SAFE_CODE_PATTERN.fullmatch(text) else ""


def _error_metadata(response: Any) -> tuple[str, str]:
    payload = _json_body(response)
    nested = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = _safe_value(nested.get("code") or payload.get("code"))
    error_type = _safe_value(nested.get("type") or payload.get("type"))
    return code, error_type


def _safe_path(response: Any) -> str:
    try:
        return urlparse(str(response.url or "")).path or "/"
    except Exception:
        return "/"


def auth_failure_message(stage: str, response: Any, *, identifier: str = "") -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    code, error_type = _error_metadata(response)
    logger.warning(
        "OpenAI 自动登录上游失败: stage=%s status=%s path=%s code=%s type=%s identifier=%s",
        stage,
        status,
        _safe_path(response),
        code or "unknown",
        error_type or "unknown",
        identifier or "unknown",
    )
    if code in CODE_MESSAGES:
        return f"{CODE_MESSAGES[code]}（OpenAI: {code}，HTTP {status}）"
    label = STAGE_LABELS.get(stage, "OpenAI 登录失败")
    suffix = f"，OpenAI: {code}" if code else ""
    return f"{label}（HTTP {status}{suffix}）"
