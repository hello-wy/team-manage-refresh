"""官key 管理页面、管理操作及管理员密码鉴权的对外接口。"""
from datetime import datetime
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.database import get_db
from app.dependencies.auth import require_admin
from app.models import Setting
from app.official_key_model import OfficialKey
from app.routes.admin import build_admin_base_context
from app.services import official_keys
from app.services.auth import auth_service
from app.services.encryption import encryption_service
from app.services.official_key_balance import OfficialKeyBalanceError, OfficialKeyBalanceService
from app.services.official_key_expiration import expiration_views, read_expirations, save_manual_expiration
from app.services.official_key_export import OfficialKeyExportService, statuses
from app.services.official_key_sub2api import OfficialKeySub2api
from app.services.sub2api import Sub2apiError, Sub2apiImportUncertain, sub2api_service

router = APIRouter(tags=["official-keys"])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class OfficialKeyRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, hide_input_in_errors=True)

    account: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


class OfficialKeyExpirationRequest(BaseModel):
    expires_at: datetime | None


def get_key_cipher():
    return encryption_service


def get_export_service(cipher=Depends(get_key_cipher)):
    return OfficialKeyExportService(gateway=OfficialKeySub2api(sub2api_service), cipher=cipher)


def get_password_verifier():
    return auth_service.verify_password


def get_balance_service():
    return OfficialKeyBalanceService()


async def require_admin_token(*, admintoken: str | None = Header(default=None),
                              db=Depends(get_db), verify=Depends(get_password_verifier)):
    if not admintoken:
        raise HTTPException(401, "请在 admintoken 请求头中提供后台登录密码")
    password_hash = (await db.execute(select(Setting.value).where(
        Setting.key == "admin_password_hash"
    ))).scalar_one_or_none()
    if not password_hash:
        raise HTTPException(503, "管理员密码尚未初始化")
    if not await run_in_threadpool(verify, admintoken, password_hash):
        raise HTTPException(401, "admintoken 无效")


@router.get("/admin/official-keys", response_class=HTMLResponse)
async def official_keys_page(request: Request, *, db=Depends(get_db), user=Depends(require_admin)):
    from app.main import templates

    context = await build_admin_base_context(
        request=request, db=db, current_user=user, active_page="official_keys"
    )
    return templates.TemplateResponse(request, "admin/official_keys.html", context, headers=NO_STORE)


@router.get("/admin/official-keys/data", dependencies=[Depends(require_admin)])
async def admin_list_keys(db=Depends(get_db), cipher=Depends(get_key_cipher),
                          service=Depends(get_balance_service)):
    from app.main import templates

    entries = await official_keys.list_keys(db, cipher=cipher)
    exports = await statuses(db)
    balances = await service.query_many({
        entry["id"]: entry["api_key"] for entry in entries
    })
    render_time = templates.env.get_template("components/team_expiration.html").module.team_expiration
    expirations = expiration_views(await read_expirations(db, entries=entries), render_time=render_time)
    return JSONResponse({"success": True, "keys": entries, "exports": exports,
                         "expirations": expirations, "balances": balances}, headers=NO_STORE)


@router.put("/admin/official-keys/{entry_id}/expiration", dependencies=[Depends(require_admin)])
async def set_expiration(entry_id: int, payload: OfficialKeyExpirationRequest, *, db=Depends(get_db)):
    await save_manual_expiration(db, entry_id=entry_id, expires_at=payload.expires_at)
    return JSONResponse({"success": True}, headers=NO_STORE)


@router.get("/admin/official-keys/{entry_id}/balance", dependencies=[Depends(require_admin)])
async def query_balance(entry_id: int, *, db=Depends(get_db), cipher=Depends(get_key_cipher),
                        service=Depends(get_balance_service)):
    entry = await db.get(OfficialKey, entry_id)
    if not entry:
        raise HTTPException(404, "官key 不存在")
    api_key = cipher.decrypt_token(entry.api_key_encrypted)
    try:
        balance = await service.query(api_key)
    except OfficialKeyBalanceError as exc:
        raise HTTPException(502, str(exc)) from exc
    return JSONResponse({"success": True, "balance": balance}, headers=NO_STORE)


@router.post("/admin/official-keys/{entry_id}/sub2api", dependencies=[Depends(require_admin)])
async def push_key(entry_id: int, *, db=Depends(get_db), service=Depends(get_export_service)):
    try:
        result = await service.push(entry_id, db)
    except Sub2apiImportUncertain as exc:
        raise HTTPException(409, str(exc)) from exc
    except Sub2apiError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse({"success": True, "export": result}, headers=NO_STORE)


@router.post("/admin/official-keys", dependencies=[Depends(require_admin)])
async def add_key(payload: OfficialKeyRequest, *, db=Depends(get_db), cipher=Depends(get_key_cipher)):
    entry = await official_keys.create_key(db, payload=payload, cipher=cipher)
    return JSONResponse({"success": True, "key": entry}, status_code=201, headers=NO_STORE)


@router.put("/admin/official-keys/{entry_id}", dependencies=[Depends(require_admin)])
async def edit_key(entry_id: int, payload: OfficialKeyRequest, *,
                   db=Depends(get_db), cipher=Depends(get_key_cipher)):
    entry = await official_keys.update_key(db, entry_id=entry_id, payload=payload, cipher=cipher)
    return JSONResponse({"success": True, "key": entry}, headers=NO_STORE)


@router.delete("/admin/official-keys/{entry_id}", dependencies=[Depends(require_admin)])
async def remove_key(entry_id: int, db=Depends(get_db)):
    await official_keys.delete_key(db, entry_id=entry_id)
    return JSONResponse({"success": True}, headers=NO_STORE)


@router.get("/api/official-keys", dependencies=[Depends(require_admin_token)])
async def export_keys(db=Depends(get_db), cipher=Depends(get_key_cipher)):
    entries = await official_keys.list_keys(db, cipher=cipher)
    return JSONResponse({"success": True, "keys": entries}, headers=NO_STORE)
