"""Admin endpoints for quota rotation."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.database import get_db
from app.dependencies.auth import require_admin
from app.models import RotationAction, Team, TeamReplacementQueue
from app.routes.admin import build_admin_base_context, team_service
from app.services.account_pool import account_pool_service
from app.services.account_pool_usage import account_pool_usage_service
from app.services.quota_rotation import RotationDependencies, run_team
from app.services.quota_sync import refresh_team
from app.services.rotation_read_model import load_team_rotation

router = APIRouter(prefix="/admin", tags=["rotation"])


class RotationConfig(BaseModel):
    mode: str


async def _team(db, team_id):
    team = await db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team 不存在")
    return team


async def _team_view(db, team):
    live = await team_service.get_team_members(team.id, db)
    balance = live.get("seat_balance") if live.get("success") else {"success": False,
                                                                  "error": live.get("error")}
    return await load_team_rotation(db, team, balance)


@router.get("/rotation", response_class=HTMLResponse)
async def rotation_page(request: Request, db=Depends(get_db), user=Depends(require_admin)):
    from app.main import templates
    teams = (await db.execute(select(Team).order_by(Team.id))).scalars().all()
    views = [await load_team_rotation(db, team) for team in teams]
    context = await build_admin_base_context(request, db, user, "rotation")
    context["rotation_teams"] = views
    return templates.TemplateResponse(request, "admin/rotation/index.html", context)


@router.get("/rotation/data")
async def rotation_data(db=Depends(get_db), user=Depends(require_admin)):
    teams = (await db.execute(select(Team).order_by(Team.id))).scalars().all()
    return {"teams": [await load_team_rotation(db, team) for team in teams]}


@router.post("/teams/{team_id}/rotation/config")
async def rotation_config(team_id: int, payload: RotationConfig, db=Depends(get_db),
                          user=Depends(require_admin)):
    if payload.mode not in {"off", "dry_run", "auto"}:
        raise HTTPException(422, "无效的轮转模式")
    team = await _team(db, team_id)
    if payload.mode != "off":
        queued = (await db.execute(select(TeamReplacementQueue.id).where(
            TeamReplacementQueue.team_id == team_id).limit(1))).scalar_one_or_none()
        if queued or team.pending_replacements:
            raise HTTPException(409, "请先处理原自动补位队列，再开启轮转")
    team.rotation_mode = payload.mode
    await db.commit()
    return {"team_id": team_id, "mode": team.rotation_mode}


@router.post("/teams/{team_id}/rotation/preview")
async def rotation_preview(team_id: int, db=Depends(get_db), user=Depends(require_admin)):
    team = await _team(db, team_id)
    return await _team_view(db, team)


@router.post("/teams/{team_id}/rotation/refresh")
async def rotation_refresh(team_id: int, db=Depends(get_db), user=Depends(require_admin)):
    team = await _team(db, team_id)
    count = await refresh_team(db, team, team_service, usage_service=account_pool_usage_service)
    return {"refreshed": count}


@router.post("/teams/{team_id}/rotation/run")
async def rotation_run(team_id: int, db=Depends(get_db), user=Depends(require_admin)):
    team = await _team(db, team_id)
    if team.rotation_mode == "off":
        raise HTTPException(409, "请先开启轮转")
    return await run_team(db, team, RotationDependencies(
        team_service, account_pool_usage_service, account_pool_service))


@router.get("/teams/{team_id}/rotation/actions")
async def rotation_actions(team_id: int, db=Depends(get_db), user=Depends(require_admin)):
    await _team(db, team_id)
    actions = (await db.execute(select(RotationAction).where(
        RotationAction.team_id == team_id).order_by(RotationAction.id.desc()).limit(100)
    )).scalars().all()
    return {"actions": [{"id": row.id, "email": row.email,
                         "action": row.action_type, "status": row.status,
                         "reason": row.reason, "result": row.result,
                         "created_at": row.created_at.isoformat()} for row in actions]}
