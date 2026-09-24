"""Rotate an account-pool entry's ChatGPT TOTP factor."""
from __future__ import annotations

from typing import Any

from app.services.account_pool_authorization import AccountPoolAuthorizationError
from app.services.account_pool_credentials import AccountPoolCredentialError
from app.utils.totp import generate_totp, normalize_totp_secret

MFA_BASE_URL = "https://chatgpt.com/backend-api/accounts"
SUCCESS_STATUS = 200


class AccountPoolTotpError(ValueError):
    def __init__(self, message: str, *, new_secret: str = ""):
        super().__init__(message)
        self.new_secret = new_secret


def _totp_factor_ids(info: dict[str, Any]) -> list[str]:
    factors = info.get("factors")
    if not isinstance(factors, dict) or not isinstance(factors.get("totp"), list):
        raise AccountPoolTotpError("2FA 信息缺少 TOTP 因子列表")
    ids = []
    for factor in factors["totp"]:
        if not isinstance(factor, dict) or not str(factor.get("id") or "").strip():
            raise AccountPoolTotpError("2FA 信息包含无效的 TOTP 因子 ID")
        ids.append(str(factor["id"]).strip())
    return ids


class AccountPoolTotpService:
    def __init__(self, authorization, credentials, remote):
        self._authorization = authorization
        self._credentials = credentials
        self._remote = remote

    async def rotate(self, db, entry_id: int) -> str:
        identifier = f"account-pool-login-{entry_id}"
        try:
            login = await self._authorization.login_entry(db, entry_id)
            token = login.payload["accounts"][0]["credentials"]["access_token"]
            session = await self._remote._get_session(db, identifier)
            headers = self._headers(session, token)
            original = await self._info(session, headers)
            old_ids = _totp_factor_ids(original)
            await self._disable_factors(login, session, headers, old_ids)
            disabled = await self._info(session, headers)
            if _totp_factor_ids(disabled):
                raise AccountPoolTotpError("旧 2FA 禁用后仍存在 TOTP 因子")
            try:
                secret = await self._enroll_and_activate(session, headers)
            except Exception as exc:
                if old_ids:
                    raise AccountPoolTotpError(f"旧 2FA 已禁用，新 2FA 创建失败：{exc}") from exc
                raise
            try:
                active = await self._info(session, headers)
                new_ids = _totp_factor_ids(active)
            except Exception as exc:
                raise AccountPoolTotpError("新 2FA 已激活，但无法复查远端状态", new_secret=secret) from exc
            enabled = active.get("mfa_enabled") or active.get("mfa_enabled_v2")
            if not enabled or not new_ids or set(new_ids) & set(old_ids):
                raise AccountPoolTotpError("新 2FA 已激活，但远端状态未确认", new_secret=secret)
            try:
                saved = await self._credentials.update_credentials(
                    db, entry_id, two_factor_secret=secret
                )
            except Exception as exc:
                raise AccountPoolTotpError("新 2FA 已激活，但本地保存失败", new_secret=secret) from exc
            if saved is None:
                raise AccountPoolTotpError("新 2FA 已激活，但账号池记录不存在", new_secret=secret)
            return secret
        except (AccountPoolAuthorizationError, AccountPoolCredentialError) as exc:
            raise AccountPoolTotpError(str(exc)) from exc
        finally:
            await self._remote.clear_session(identifier)

    @staticmethod
    def _headers(session, token: str) -> dict[str, str]:
        device_id = str(session.cookies.get("oai-did") or "").strip()
        if not device_id:
            raise AccountPoolTotpError("登录会话缺少设备 ID")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://chatgpt.com/",
            "oai-device-id": device_id,
            "oai-language": "zh-CN",
        }

    async def _disable_factors(self, login, session, headers, factor_ids: list[str]) -> None:
        if not factor_ids:
            return
        if len(factor_ids) != 1 or login.verified_totp_factor_id != factor_ids[0]:
            raise AccountPoolTotpError("已验证的 TOTP 因子与当前 2FA 信息不一致，拒绝禁用")
        await self._post(session, headers, "mfa/user/disable_in_house", {
            "factor_id": factor_ids[0],
        })

    @staticmethod
    async def _info(session, headers: dict[str, str]) -> dict[str, Any]:
        response = await session.get(f"{MFA_BASE_URL}/mfa_info", headers=headers)
        if response.status_code != SUCCESS_STATUS:
            raise AccountPoolTotpError(f"获取 2FA 信息失败（HTTP {response.status_code}）")
        payload = response.json()
        if not isinstance(payload, dict):
            raise AccountPoolTotpError("2FA 信息响应格式无效")
        return payload

    @staticmethod
    async def _post(session, headers: dict[str, str], path: str, payload: dict) -> dict:
        response = await session.post(f"{MFA_BASE_URL}/{path}", headers=headers, json=payload)
        if response.status_code != SUCCESS_STATUS:
            raise AccountPoolTotpError(f"{path} 失败（HTTP {response.status_code}）")
        data = response.json()
        if not isinstance(data, dict) or data.get("success") is False:
            raise AccountPoolTotpError(f"{path} 响应未确认成功")
        return data

    async def _enroll_and_activate(self, session, headers: dict[str, str]) -> str:
        enrollment = await self._post(session, headers, "mfa/enroll", {"factor_type": "totp"})
        secret = normalize_totp_secret(str(enrollment.get("secret") or ""))
        session_id = str(enrollment.get("session_id") or "").strip()
        if not secret or not session_id:
            raise AccountPoolTotpError("创建 2FA 未返回密钥或 session_id")
        code = generate_totp(secret)
        activation = await self._post(session, headers, "mfa/user/activate_enrollment", {
            "code": code, "factor_type": "totp", "session_id": session_id,
        })
        if activation.get("success") is not True:
            raise AccountPoolTotpError("新 2FA 激活响应未确认成功")
        return secret
