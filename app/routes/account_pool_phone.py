"""Phone verification workspace for existing account-pool entries."""
import logging
import re

import httpx

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_db
from app.dependencies.auth import require_admin
from app.models import AccountPoolEntry
from app.routes.admin import account_pool_authorization_service, build_admin_base_context
from app.services.encryption import encryption_service
from app.services.settings import settings_service
from app.services.operation_progress import stream_operation
from app.services.phone_verification import HTTP_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/phone-verification", tags=["admin-phone-verification"])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
DEFAULT_ENDPOINT = "https://hero-sms.com/stubs/handler_api.php"


class PhoneVerificationSettings(BaseModel):
    api_key: str = Field(default="", max_length=512)
    country: str = Field(default="38", min_length=1, max_length=10)
    max_price: str = Field(default="0.06", max_length=16)

    @field_validator("country")
    @classmethod
    def validate_country(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("国家编号只能包含数字")
        return value

    @field_validator("max_price")
    @classmethod
    def validate_price(cls, value: str) -> str:
        if value and not re.fullmatch(r"\d+(\.\d{1,6})?", value):
            raise ValueError("价格上限格式无效")
        return value


class HeroSMSCountry(BaseModel):
    id: int = Field(ge=0)
    chn: str
    eng: str
    visible: int


def _country_options(payload):
    if not isinstance(payload, dict) or not payload:
        raise ValueError("HeroSMS 国家列表格式无效")
    countries = [HeroSMSCountry.model_validate(item) for item in payload.values()]
    options = []
    for country in countries:
        if not country.visible:
            continue
        name = country.chn.strip() or country.eng.strip()
        if not name:
            raise ValueError(f"HeroSMS 国家 {country.id} 缺少名称")
        options.append({"id": str(country.id), "name": name})
    if not options:
        raise ValueError("HeroSMS 未返回可用国家")
    return sorted(options, key=lambda item: int(item["id"]))


@router.get("/countries")
async def phone_verification_countries(current_user: dict = Depends(require_admin)):
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(DEFAULT_ENDPOINT, params={"action": "getCountries"})
        response.raise_for_status()
        countries = _country_options(response.json())
    except (httpx.HTTPError, ValueError):
        logger.exception("读取 HeroSMS 国家列表失败")
        return JSONResponse(status_code=502, content={
            "success": False, "error": "读取 HeroSMS 国家列表失败，请刷新页面重试",
        }, headers=NO_STORE)
    return JSONResponse(content={"success": True, "countries": countries}, headers=NO_STORE)


@router.get("", response_class=HTMLResponse)
async def phone_verification_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.main import templates

    entries = (await db.execute(
        select(AccountPoolEntry)
        .where(AccountPoolEntry.deleted_at.is_(None))
        .order_by(AccountPoolEntry.email)
    )).scalars().all()
    rows = []
    for entry in entries:
        phone = encryption_service.decrypt_token(entry.phone_number_encrypted) if entry.phone_number_encrypted else ""
        rows.append({
            "id": entry.id,
            "email": entry.email,
            "phone": _mask_phone(phone),
            "verified_at": entry.phone_verified_at,
        })
    context = await build_admin_base_context(request, db, current_user, "phone_verification")
    context.update({
        "phone_entries": rows,
        "herosms_configured": bool(await settings_service.get_setting(db, "herosms_api_key_encrypted", "")),
        "herosms_country": await settings_service.get_setting(db, "herosms_country", "38"),
        "herosms_max_price": await settings_service.get_setting(db, "herosms_max_price", "0.06"),
    })
    return templates.TemplateResponse(request, "admin/phone_verification/index.html", context)


@router.put("/settings")
async def save_phone_verification_settings(
    payload: PhoneVerificationSettings,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    encrypted_key = ""
    if payload.api_key.strip():
        encrypted_key = encryption_service.encrypt_token(payload.api_key.strip())
    else:
        encrypted_key = await settings_service.get_setting(db, "herosms_api_key_encrypted", "")
        if not encrypted_key:
            return JSONResponse(status_code=400, content={
                "success": False, "error": "首次配置需要填写 HeroSMS API Key",
            }, headers=NO_STORE)
    saved = await settings_service.update_settings(db, {
        "herosms_api_key_encrypted": encrypted_key,
        "herosms_endpoint": DEFAULT_ENDPOINT,
        "herosms_country": payload.country,
        "herosms_max_price": payload.max_price,
    })
    if not saved:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "保存 HeroSMS 设置失败"},
            headers=NO_STORE,
        )
    return JSONResponse(content={"success": True}, headers=NO_STORE)


@router.post("/{entry_id}")
async def add_phone_to_pool_account(
    entry_id: int,
    current_user: dict = Depends(require_admin),
):
    async def operation(report):
        async with AsyncSessionLocal() as db:
            return await _run_phone_verification(entry_id, db, report=report)

    return StreamingResponse(
        stream_operation(operation),
        media_type="application/x-ndjson",
        headers={**NO_STORE, "X-Accel-Buffering": "no", "Content-Encoding": "identity"},
    )


async def _run_phone_verification(entry_id, db, *, report):
    try:
        await report("login")
        content = await _verify_and_save(entry_id, db, report=report)
        return {"type": "result", "success": True, **content}
    except (ValueError, KeyError) as exc:
        await db.rollback()
        return {"type": "error", "success": False, "error": str(exc)}
    except httpx.HTTPStatusError as exc:
        await db.rollback()
        logger.error("手机号验证上游请求失败 (entry=%s, status=%s, path=%s)",
                     entry_id, exc.response.status_code, exc.request.url.path)
        return {"type": "error", "success": False,
                "error": f"上游请求失败（HTTP {exc.response.status_code}）"}
    except Exception:
        await db.rollback()
        logger.exception("号池账号手机号验证失败 (entry=%s)", entry_id)
        return {"type": "error", "success": False,
                "error": "手机号验证失败，请检查服务端日志"}


async def _verify_and_save(entry_id, db, *, report):
    entry = await db.get(AccountPoolEntry, entry_id)
    if entry is None or entry.deleted_at is not None:
        raise ValueError("账号不存在")
    credential_version = (entry.password_encrypted, entry.two_factor_secret_encrypted)
    result = await account_pool_authorization_service.login_entry(
        db, entry_id, entry.workspace_id or "", phone_verification=True,
        on_progress=report,
    )
    if not result.phone_number:
        raise ValueError("登录流程没有要求添加手机号；远端账号状态未改变")
    await report("save")
    saved = await account_pool_authorization_service.save_result(
        db, entry_id, result, credential_version=credential_version
    )
    if not saved:
        raise ValueError("手机号验证已完成，但账号号池记录已变化，未保存结果")
    content = {"phone": _mask_phone(result.phone_number)}
    if result.phone_rental_error:
        content["warning"] = f"手机号已验证并保存，但 HeroSMS 租号结束确认失败：{result.phone_rental_error}"
    return content


def _mask_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) <= 6:
        return value
    return f"+{digits[:3]}****{digits[-3:]}"
