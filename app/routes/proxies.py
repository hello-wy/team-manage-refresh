"""Authenticated proxy inventory and per-account outbound routing."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.models import AccountProxyRoute, ProxyEndpoint
from app.routes.admin import build_admin_base_context
from app.services.encryption import encryption_service
from app.services.proxy_manager import (
    DEFAULT_ROUTE, known_accounts, proxy_probe, public_proxy, save_route, validate_managed_proxy,
)
from app.services.settings import settings_service
from app.utils.proxy import mask_proxy_url

router = APIRouter(prefix="/admin/proxies", tags=["admin-proxies"], dependencies=[Depends(require_admin)])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class ProxyInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: SecretStr | None = None
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("请输入有效代理名称")
        return value


class RouteInput(BaseModel):
    mode: Literal["inherit", "direct", "proxy"]
    proxy_id: int | None = Field(default=None, gt=0)


class AccountRouteInput(RouteInput):
    email: str = Field(min_length=3, max_length=255)


@router.get("", response_class=HTMLResponse)
async def proxy_page(request: Request, db: AsyncSession = Depends(get_db), user: dict = Depends(require_admin)):
    from app.main import templates
    context = await build_admin_base_context(request, db, user, "proxies")
    return templates.TemplateResponse(request, "admin/proxies/index.html", context, headers=NO_STORE)


@router.get("/data")
async def proxy_data(db: AsyncSession = Depends(get_db)):
    proxies = (await db.scalars(select(ProxyEndpoint).order_by(ProxyEndpoint.id))).all()
    default = await db.get(AccountProxyRoute, DEFAULT_ROUTE)
    legacy = await settings_service.get_proxy_config(db)
    return JSONResponse({
        "success": True, "proxies": [await public_proxy(db, proxy) for proxy in proxies],
        "accounts": await known_accounts(db),
        "default": {"mode": default.mode if default else "inherit", "proxy_id": default.proxy_id if default else None},
        "legacy": {"enabled": legacy["enabled"], "address": mask_proxy_url(legacy["proxy"]) if legacy["proxy"] else ""},
    }, headers=NO_STORE)


async def persist_proxy(db, payload, proxy=None):
    try:
        raw = payload.url.get_secret_value().strip() if payload.url is not None else ""
        if raw:
            raw = validate_managed_proxy(raw)
        elif proxy is None:
            raise ValueError("新增代理时必须填写完整代理地址")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if proxy is None:
        proxy = ProxyEndpoint(name=payload.name)
        db.add(proxy)
    if raw:
        try:
            old = encryption_service.decrypt_token(proxy.url_encrypted) if proxy.url_encrypted else None
        except Exception:
            old = None
        if raw != old:
            proxy.url_encrypted = encryption_service.encrypt_token(raw)
            proxy.test_status = "untested"
            for key in ("exit_ip", "country", "region", "city", "latency_ms", "test_message", "tested_at"):
                setattr(proxy, key, None)
    proxy.name, proxy.enabled = payload.name, payload.enabled
    await db.commit()
    return JSONResponse({"success": True, "proxy": await public_proxy(db, proxy)}, headers=NO_STORE)


@router.post("/nodes")
async def create_proxy(payload: ProxyInput, db: AsyncSession = Depends(get_db)):
    return await persist_proxy(db, payload)


@router.put("/nodes/{proxy_id}")
async def update_proxy(proxy_id: int, payload: ProxyInput, db: AsyncSession = Depends(get_db)):
    proxy = await db.get(ProxyEndpoint, proxy_id)
    if proxy is None:
        raise HTTPException(404, "代理不存在")
    return await persist_proxy(db, payload, proxy)


@router.delete("/nodes/{proxy_id}")
async def delete_proxy(proxy_id: int, db: AsyncSession = Depends(get_db)):
    proxy = await db.get(ProxyEndpoint, proxy_id)
    if proxy is None:
        raise HTTPException(404, "代理不存在")
    count = await db.scalar(select(func.count()).select_from(AccountProxyRoute).where(
        AccountProxyRoute.proxy_id == proxy_id,
    ))
    if count:
        raise HTTPException(409, "该代理仍用于账号或默认出口，请先调整绑定再删除")
    await db.delete(proxy)
    await db.commit()
    return JSONResponse({"success": True}, headers=NO_STORE)


@router.post("/nodes/{proxy_id}/test")
async def test_proxy(proxy_id: int, db: AsyncSession = Depends(get_db)):
    if await db.get(ProxyEndpoint, proxy_id) is None:
        raise HTTPException(404, "代理不存在")
    try:
        result = await proxy_probe.test(db, proxy_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return JSONResponse({"success": True, "proxy": result}, headers=NO_STORE)


@router.put("/default")
async def update_default(payload: RouteInput, db: AsyncSession = Depends(get_db)):
    try:
        await save_route(db, DEFAULT_ROUTE, payload.mode, payload.proxy_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse({"success": True}, headers=NO_STORE)


@router.put("/accounts")
async def update_account_route(payload: AccountRouteInput, db: AsyncSession = Depends(get_db)):
    if payload.email.strip() == DEFAULT_ROUTE:
        raise HTTPException(400, "请使用默认出口设置")
    try:
        await save_route(db, payload.email, payload.mode, payload.proxy_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse({"success": True}, headers=NO_STORE)
