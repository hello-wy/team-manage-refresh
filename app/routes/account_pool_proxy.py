"""Configure account login routes from the account pool."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.models import AccountPoolEntry, AccountProxyRoute, ProxyEndpoint, Team
from app.services.proxy_manager import DEFAULT_ROUTE, attach_login_proxy_labels, public_proxy, save_route
from app.services.settings import settings_service
from app.utils.proxy import mask_proxy_url

router = APIRouter(prefix="/admin/account-pool", tags=["account-pool-proxy"],
                   dependencies=[Depends(require_admin)])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class RouteInput(BaseModel):
    mode: Literal["inherit", "direct", "proxy"]
    proxy_id: int | None = Field(default=None, gt=0)


async def account_email(db, entry_id):
    record = await db.get(Team if entry_id < 0 else AccountPoolEntry, abs(entry_id))
    if record is None or (entry_id >= 0 and record.deleted_at is not None):
        raise HTTPException(404, "账号不存在")
    return record.email.strip().lower()


def route_data(route):
    return {"mode": route.mode if route else "inherit",
            "proxy_id": route.proxy_id if route else None}


@router.get("/proxy-options")
async def proxy_options(entry_id: int | None = None, db: AsyncSession = Depends(get_db)):
    email = await account_email(db, entry_id) if entry_id is not None else None
    proxies = (await db.scalars(select(ProxyEndpoint).order_by(ProxyEndpoint.id))).all()
    default = await db.get(AccountProxyRoute, DEFAULT_ROUTE)
    legacy = await settings_service.get_proxy_config(db)
    account = {"email": email, **route_data(await db.get(AccountProxyRoute, email))} if email else None
    return JSONResponse({
        "success": True, "proxies": [await public_proxy(db, proxy) for proxy in proxies],
        "default": route_data(default), "account": account,
        "legacy": {"enabled": legacy["enabled"],
                   "address": mask_proxy_url(legacy["proxy"]) if legacy["proxy"] else ""},
    }, headers=NO_STORE)


@router.put("/proxy-default")
async def update_default(payload: RouteInput, db: AsyncSession = Depends(get_db)):
    try:
        await save_route(db, DEFAULT_ROUTE, payload.mode, payload.proxy_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse({"success": True}, headers=NO_STORE)


@router.put("/{entry_id}/proxy")
async def update_account_proxy(entry_id: int, payload: RouteInput, db: AsyncSession = Depends(get_db)):
    email = await account_email(db, entry_id)
    try:
        await save_route(db, email, payload.mode, payload.proxy_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    rows = await attach_login_proxy_labels(db, [{"email": email}])
    return JSONResponse({"success": True, "login_proxy": rows[0]["login_proxy"]}, headers=NO_STORE)
