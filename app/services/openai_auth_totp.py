"""Complete the TOTP challenge during automatic OAuth login."""
from typing import Any

from app.services.openai_auth_errors import auth_failure_message
from app.services.openai_auth_protocol import (
    AUTH_ORIGIN, auth_headers as _auth_headers, mfa_factor_id as _mfa_factor_id,
    next_url as _next_url,
)
from app.services.openai_auth_session import auth_session_claims
from app.services.openai_automatic_login_models import OpenAIAutomaticLoginError
from app.utils.totp import TotpError, generate_totp


async def complete_totp(
    session: Any, secret: str, *, payload: dict[str, Any], referer: str,
    device_id: str, sentinel_token: str,
) -> tuple[str, str]:
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
    return _next_url(verify), factor_id

