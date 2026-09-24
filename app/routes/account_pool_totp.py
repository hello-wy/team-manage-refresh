"""Admin endpoint for rotating an account-pool TOTP factor."""
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.routes.admin import account_pool_authorization_service, team_service
from app.services.account_pool_credentials import account_pool_credential_service
from app.services.account_pool_totp import AccountPoolTotpError, AccountPoolTotpService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/account-pool", tags=["admin-account-pool"])
NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
totp_service = AccountPoolTotpService(
    account_pool_authorization_service,
    account_pool_credential_service,
    team_service.chatgpt_service,
)


@router.post("/{entry_id}/rotate-2fa")
async def rotate_account_pool_totp(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        secret = await totp_service.rotate(db, entry_id)
    except AccountPoolTotpError as exc:
        await db.rollback()
        content = {"success": False, "error": str(exc)}
        if exc.new_secret:
            content["two_factor_secret"] = exc.new_secret
        return JSONResponse(status_code=400, content=content, headers=NO_STORE_HEADERS)
    except Exception:
        await db.rollback()
        logger.exception("账号池 2FA 更换失败 (entry=%s)", entry_id)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "更换 2FA 失败，请检查服务端日志"},
            headers=NO_STORE_HEADERS,
        )
    return JSONResponse(
        content={"success": True, "two_factor_secret": secret, "message": "2FA 已更换"},
        headers=NO_STORE_HEADERS,
    )
