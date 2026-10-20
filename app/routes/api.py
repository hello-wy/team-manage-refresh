"""
API 路由
处理 AJAX 请求的 API 端点
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.services.team import TeamService
from app.services.account_pool_credentials import account_pool_credential_service
from app.services.encryption import encryption_service
from app.services.openai_automatic_login import AutomaticLoginDependencies, OpenAIAutomaticLoginService
from app.services.team_refresh import TeamRefreshDependencies, TeamRefreshService

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(
    prefix="/api",
    tags=["api"]
)

# 服务实例
team_service = TeamService()
team_refresh_service = TeamRefreshService(TeamRefreshDependencies(
    teams=team_service, credentials=account_pool_credential_service,
    login=OpenAIAutomaticLoginService(AutomaticLoginDependencies(
        get_session=team_service.chatgpt_service._get_session,
        clear_session=team_service.chatgpt_service.clear_session,
        exchange_code=team_service.chatgpt_service.exchange_oauth_code,
    )),
    cipher=encryption_service, jwt=team_service.jwt_parser,
))


@router.get("/teams/{team_id}/refresh")
async def refresh_team(
    team_id: int,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    刷新 Team 信息；异常登录状态使用账号号池密码和 2FA 重新登录一次。

    Args:
        team_id: Team ID
        force: 是否强制刷新 Token
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        刷新结果
    """
    try:
        logger.info(f"刷新 Team {team_id} 信息, force={force}")

        result = await team_refresh_service.refresh(team_id, db, force_refresh=force)

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception:
        logger.exception("刷新 Team 失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error_code": "unknown_error",
                "error": "刷新 Team 信息时发生异常，请稍后重试"
            }
        )
