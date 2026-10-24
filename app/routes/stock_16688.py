"""管理员 16688 补货与配置接口。"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.services.stock_16688 import DEFAULT_GATEWAY, Stock16688Error, save_settings, stock_16688_service

router = APIRouter(prefix='/admin', tags=['admin-stock-16688'])
NO_STORE = {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


class Stock16688SettingsRequest(BaseModel):
    gateway: str = Field(DEFAULT_GATEWAY, max_length=2048)
    app_id: str = Field(..., min_length=1, max_length=255)
    goods_no: str = Field(..., min_length=1, max_length=255)
    secret: str = Field('', max_length=4096)


@router.post('/settings/16688')
async def update_settings(payload: Stock16688SettingsRequest, db: AsyncSession = Depends(get_db),
                          current_user: dict = Depends(require_admin)):
    try:
        result = await save_settings(db, **payload.model_dump())
        return JSONResponse(content=result, headers=NO_STORE)
    except Stock16688Error as exc:
        return JSONResponse(status_code=exc.status_code, content={'success': False, 'error': str(exc)}, headers=NO_STORE)


@router.post('/account-pool/{entry_id}/import-16688')
async def import_entry(entry_id: int, db: AsyncSession = Depends(get_db),
                       current_user: dict = Depends(require_admin)):
    try:
        result = await stock_16688_service.import_entry(db, entry_id)
        return JSONResponse(content=result, headers=NO_STORE)
    except Stock16688Error as exc:
        return JSONResponse(status_code=exc.status_code, content={'success': False, 'error': str(exc)}, headers=NO_STORE)
