"""HeroSMS-backed phone verification for an account-pool OAuth login."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.encryption import encryption_service
from app.services.openai_auth_protocol import AUTH_ORIGIN, auth_headers
from app.services.settings import settings_service
from app.services.operation_progress import ProgressCallback, report_progress

HEROSMS_DEFAULT_ENDPOINT = "https://hero-sms.com/stubs/handler_api.php"
OPENAI_SMS_SERVICE = "dr"
SMS_WAIT_SECONDS = 120
SMS_POLL_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 20


class PhoneVerificationError(ValueError):
    pass


class SmsCodeTimeout(PhoneVerificationError):
    """The rented number did not receive an SMS within the waiting period."""


@dataclass(frozen=True)
class RentalSettings:
    api_key: str
    endpoint: str
    country: str
    max_price: str


@dataclass(frozen=True)
class VerificationContext:
    session: object
    device_id: str
    sentinel_token: str
    current_url: str
    progress: ProgressCallback | None


async def complete_herosms_phone_verification(
    request, session, device_id: str, sentinel_token: str, current_url: str
) -> dict[str, str]:
    provider = await _load_rental_settings(request.db_session)
    context = VerificationContext(session, device_id, sentinel_token, current_url, request.on_progress)
    activation_id, phone_number, code = await _receive_code(provider, context)
    try:
        await report_progress(context.progress, "verify_code")
        continue_url = await _submit_code(session, device_id, sentinel_token, code)
        await report_progress(context.progress, "finish_rental")
    except (Exception, asyncio.CancelledError) as original_error:
        await _cancel_rental(provider, activation_id, original_error)
        raise
    cleanup_error = ""
    try:
        await _finish_activation(provider.api_key, provider.endpoint, activation_id, "6")
    except Exception as exc:
        cleanup_error = str(exc)
    return {
        "phone_number": phone_number,
        "continue_url": continue_url,
        "rental_cleanup_error": cleanup_error,
    }


async def _load_rental_settings(db: AsyncSession) -> RentalSettings:
    encrypted_key = await settings_service.get_setting(db, "herosms_api_key_encrypted", "")
    if not encrypted_key:
        raise PhoneVerificationError("请先在手机号接码页面配置 HeroSMS API Key")
    api_key = encryption_service.decrypt_token(encrypted_key)
    endpoint = str(await settings_service.get_setting(
        db, "herosms_endpoint", HEROSMS_DEFAULT_ENDPOINT
    ) or HEROSMS_DEFAULT_ENDPOINT).strip()
    country = str(await settings_service.get_setting(db, "herosms_country", "38") or "38").strip()
    max_price = str(await settings_service.get_setting(db, "herosms_max_price", "0.06") or "").strip()
    return RentalSettings(api_key, endpoint, country, max_price)


async def _receive_code(provider: RentalSettings, context: VerificationContext):
    while True:
        await report_progress(context.progress, "rent_number")
        activation_id, phone_number = await _rent_number(
            provider.api_key, provider.endpoint, provider.country, provider.max_price,
        )
        try:
            await report_progress(context.progress, "send_code")
            await _send_code(
                context.session, context.device_id, context.sentinel_token, context.current_url, phone_number,
            )
            await report_progress(context.progress, "wait_code")
            code = await _wait_for_code(
                provider.api_key, provider.endpoint, activation_id, context.progress,
            )
            return activation_id, phone_number, code
        except SmsCodeTimeout as timeout_error:
            try:
                await report_progress(context.progress, "cancel_number")
            finally:
                await _cancel_rental(provider, activation_id, timeout_error)
        except (Exception, asyncio.CancelledError) as original_error:
            await _cancel_rental(provider, activation_id, original_error)
            raise


async def _cancel_rental(provider: RentalSettings, activation_id: str, original_error: BaseException):
    # A browser disconnect must wait for cancellation confirmation before the worker exits.
    release = asyncio.create_task(_finish_activation(provider.api_key, provider.endpoint, activation_id, "8"))
    try:
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            await release
            raise
    except Exception as release_error:
        raise PhoneVerificationError(
            f"{original_error}；HeroSMS 取消租号失败：{release_error}"
        ) from original_error


async def _rent_number(api_key: str, endpoint: str, country: str, max_price: str):
    params = {"api_key": api_key, "action": "getNumberV2", "service": OPENAI_SMS_SERVICE, "country": country}
    if max_price:
        params["maxPrice"] = max_price
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(endpoint, params=params)
    response.raise_for_status()
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise PhoneVerificationError(f"HeroSMS 返回了无效的号码响应：{response.text[:160]}") from exc
    activation_id = str(payload.get("activationId") or payload.get("activation_id") or "").strip()
    number = str(payload.get("phoneNumber") or payload.get("phone") or "").strip()
    if not activation_id or not number:
        raise PhoneVerificationError(f"HeroSMS 未返回号码或租号 ID：{response.text[:160]}")
    return activation_id, _normalize_phone(number)


async def _send_code(session, device_id, sentinel_token, current_url, phone_number):
    response = await session.post(
        f"{AUTH_ORIGIN}/api/accounts/add-phone/send",
        headers=auth_headers(device_id, current_url, json_request=True, sentinel_token=sentinel_token),
        json={"phone_number": phone_number},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise PhoneVerificationError(f"OpenAI 发送验证码失败（HTTP {response.status_code}）")


async def _wait_for_code(
    api_key: str,
    endpoint: str,
    activation_id: str,
    progress: ProgressCallback | None = None,
) -> str:
    params = {"api_key": api_key, "action": "getStatus", "id": activation_id}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        for _ in range(SMS_WAIT_SECONDS // SMS_POLL_SECONDS):
            response = await client.get(endpoint, params=params)
            response.raise_for_status()
            result = response.text.strip()
            if result.startswith("STATUS_OK:"):
                code = result.partition(":")[2].strip().strip("'\"")
                if code:
                    return code
            elif result == "STATUS_CANCEL":
                raise PhoneVerificationError("HeroSMS 租号已取消")
            elif not result.startswith(("STATUS_WAIT_CODE", "STATUS_WAIT_RETRY:")):
                raise PhoneVerificationError(f"HeroSMS 获取验证码失败：{result[:160]}")
            await report_progress(progress, "wait_code")
            await asyncio.sleep(SMS_POLL_SECONDS)
    raise SmsCodeTimeout(f"等待 HeroSMS 验证码超时（{SMS_WAIT_SECONDS} 秒）")


async def _submit_code(session, device_id, sentinel_token, code: str) -> str:
    response = await session.post(
        f"{AUTH_ORIGIN}/api/accounts/phone-otp/validate",
        headers=auth_headers(
            device_id, f"{AUTH_ORIGIN}/phone-verification", json_request=True,
            sentinel_token=sentinel_token,
        ),
        json={"code": code},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise PhoneVerificationError(f"OpenAI 拒绝了手机验证码（HTTP {response.status_code}）")
    payload = response.json()
    continue_url = str(payload.get("continue_url") or response.headers.get("Location") or "").strip()
    if not continue_url:
        raise PhoneVerificationError("OpenAI 未返回验证码验证后的继续地址")
    return urljoin(AUTH_ORIGIN, continue_url)


async def _finish_activation(api_key: str, endpoint: str, activation_id: str, status: str) -> None:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(endpoint, params={
            "api_key": api_key, "action": "setStatus", "id": activation_id, "status": status,
        })
    response.raise_for_status()
    expected = "ACCESS_ACTIVATION" if status == "6" else "ACCESS_CANCEL"
    if response.text.strip() != expected:
        raise PhoneVerificationError(f"HeroSMS 未确认租号结束状态：{response.text[:160]}")


def _normalize_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        raise PhoneVerificationError("HeroSMS 返回了无效手机号")
    return f"+{digits}"
