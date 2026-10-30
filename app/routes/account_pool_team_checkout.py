"""账号详情生成 Team 支付链接；仅向浏览器返回支付地址。"""
import logging

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import require_admin
from app.routes import admin
from app.services.account_pool_authorization import AccountPoolAuthorizationError
from app.services.team_checkout import TeamCheckoutClient, TeamCheckoutError

router = APIRouter(prefix="/admin/account-pool", tags=["account-pool-team-checkout"])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
logger = logging.getLogger(__name__)


class TeamCheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: str = ""
    coupon: SecretStr = SecretStr("")


def get_checkout_client():
    return TeamCheckoutClient(settings, httpx.AsyncClient)


@router.post("/{entry_id}/team-checkout")
async def create_checkout(entry_id: int, payload: TeamCheckoutRequest,
                          db: AsyncSession = Depends(get_db), *,
                          current_user: dict = Depends(require_admin),
                          client: TeamCheckoutClient = Depends(get_checkout_client)):
    try:
        client.configuration()
        result = await admin._login_and_save_account_pool_entry(
            db, entry_id, admin.AccountPoolAutomaticLoginRequest(workspace_id=payload.workspace_id),
        )
        checkout = await client.create(result.payload, payload.coupon.get_secret_value())
        return JSONResponse(content={"success": True, **checkout}, headers=NO_STORE)
    except (AccountPoolAuthorizationError, TeamCheckoutError) as exc:
        await db.rollback()
        status = exc.status_code if isinstance(exc, TeamCheckoutError) else 400
        return JSONResponse(status_code=status, content={"success": False, "error": str(exc)}, headers=NO_STORE)
    except Exception:
        await db.rollback()
        logger.exception("账号池 Team 支付链接生成失败 (entry=%s)", entry_id)
        return JSONResponse(status_code=500, content={
            "success": False, "error": "生成 Team 支付链接失败，请检查服务端日志",
        }, headers=NO_STORE)
