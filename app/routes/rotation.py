"""Admin endpoints for quota rotation."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.database import get_db
from app.dependencies.auth import require_admin
from app.models import RotationAction, Team, TeamEmailMapping, TeamReplacementQueue
from app.routes.admin import build_admin_base_context, team_service
from app.services.account_pool import account_pool_service
from app.services.account_pool_usage import account_pool_usage_service
from app.services.quota_rotation import RotationDependencies, run_team
from app.services.quota_sync import refresh_team, store_quota
from app.services.rotation_read_model import load_team_rotation, snapshot_usage
from app.services.rotation_candidates import find_rotation_candidate

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
    balance = live.get("seat_balance") if live.get("success") else None
    if balance is None:
        balance = {"success": False, "error": live.get("error") or "席位余额未知"}
    view = await load_team_rotation(db, team, balance)
    standard = ((balance or {}).get("balance") or {}).get("standard", {}).get("remaining")
    if not view["next_action"] and standard and not any(
        row.get("status") == "invited" for row in live.get("members", [])
    ):
        candidate = await find_rotation_candidate(db, team, account_pool_service)
        if candidate:
            view["next_action"] = {"email": candidate.email, "action": "invite",
                                   "reason": "标准席位空缺"}
    return view


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
    try:
        await refresh_team(db, team, team_service, usage_service=account_pool_usage_service)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    from app.main import templates
    view = await load_team_rotation(db, team)
    html = templates.env.get_template("admin/rotation/_team.html").render(team=view)
    return {"html": html}


@router.post("/teams/{team_id}/rotation/members/{email}/quota")
async def rotation_member_quota(team_id: int, email: str, db=Depends(get_db),
                                user=Depends(require_admin)):
    team = await _team(db, team_id)
    normalized = email.strip().lower()
    mapping = (await db.execute(select(TeamEmailMapping).where(
        TeamEmailMapping.team_id == team_id,
        TeamEmailMapping.email == normalized,
        TeamEmailMapping.status.in_(("joined", "invited")),
    ))).scalar_one_or_none()
    owner_email = team.email.strip().lower()
    if mapping is None and normalized != owner_email:
        raise HTTPException(404, "成员不存在")
    usage = await account_pool_usage_service.check_email(db, normalized, team.account_id)
    if usage["status"] != "ok":
        raise HTTPException(502, usage.get("error") or "额度查询失败")
    snapshot = await store_quota(db, email=normalized, space_id=team.account_id,
                                 seat_type=mapping.seat_type if mapping else "unknown", usage=usage)
    await db.commit()
    from app.main import templates
    html = templates.env.get_template("admin/rotation/_quota.html").render(
        usage=snapshot_usage(snapshot))
    view = await load_team_rotation(db, team)
    member = next(row for row in view["members"] if row["email"] == normalized)
    blocked_reason = member["blocked_reason"] or (
        "额度数据过期或不可用" if not member["quota_fresh"] else "-")
    return {"html": html, "observed_at": snapshot.observed_at.isoformat(),
            "blocked_reason": blocked_reason}


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
