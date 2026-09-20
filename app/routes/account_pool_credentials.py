"""账号号池敏感凭据接口。"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.services.account_pool_credentials import (
    AccountPoolCredentialError,
    account_pool_credential_service,
)

router = APIRouter(prefix="/admin/account-pool", tags=["admin-account-pool"])
NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class AccountPoolCredentialUpdateRequest(BaseModel):
    password: Optional[str] = Field(None, max_length=4096)
    two_factor_secret: Optional[str] = Field(None, max_length=2048)

    @model_validator(mode="after")
    def require_change(self):
        if self.password is None and self.two_factor_secret is None:
            raise ValueError("请至少修改密码或 2FA")
        return self


@router.get("/credentials")
async def get_account_pool_credentials(
    email: str = Query(..., min_length=3, max_length=255),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        credentials = await account_pool_credential_service.get_credentials(db, email)
    except AccountPoolCredentialError as exc:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(exc)},
            headers=NO_STORE_HEADERS,
        )
    if credentials is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"success": False, "error": "该邮箱不在账号号池中"},
            headers=NO_STORE_HEADERS,
        )
    return JSONResponse(
        content={"success": True, "data": credentials},
        headers=NO_STORE_HEADERS,
    )


@router.patch("/{entry_id}/credentials")
async def update_account_pool_credentials(
    entry_id: int,
    payload: AccountPoolCredentialUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        credentials = await account_pool_credential_service.update_credentials(
            db,
            entry_id,
            password=payload.password,
            two_factor_secret=payload.two_factor_secret,
        )
    except AccountPoolCredentialError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"success": False, "error": str(exc)},
            headers=NO_STORE_HEADERS,
        )
    if credentials is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"success": False, "error": "账号不存在"},
            headers=NO_STORE_HEADERS,
        )
    return JSONResponse(
        content={"success": True, "message": "账号凭据已保存", "data": credentials},
        headers=NO_STORE_HEADERS,
    )


@router.delete("/{entry_id}")
async def delete_account_pool_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    deleted = await account_pool_credential_service.delete_entry(db, entry_id)
    if not deleted:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"success": False, "error": "账号不存在"},
        )
    return JSONResponse(
        content={"success": True, "message": "账号及其加入历史已从数据库删除"}
    )
