"""Admin CPA exports with the existing CliproxyAPI configuration."""
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.cpa_export_model import CpaExportJob
from app.database import AsyncSessionLocal, get_db
from app.dependencies.auth import require_admin
from app.routes.admin import account_pool_authorization_service, team_sub2api_export_service
from app.services.account_pool_authorization import AccountPoolAuthorizationError
from app.services.cliproxyapi import cliproxyapi_service
from app.services.cpa import CpaExportError, CpaService
from app.services.cpa_export import AccountPoolCpaExportService, TeamCpaExportService
from app.services.member_authorization import MemberAuthorizationError

router = APIRouter(prefix="/admin", tags=["cpa"], dependencies=[Depends(require_admin)])
logger = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class CpaExportRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    workspace_id: str = ""


def get_cpa_service():
    return CpaService(load_config=cliproxyapi_service._load_config, client_factory=httpx.AsyncClient)


def get_team_export_service(cpa=Depends(get_cpa_service)):
    return TeamCpaExportService(preparation=team_sub2api_export_service, cpa=cpa)


def get_pool_export_service(cpa=Depends(get_cpa_service)):
    return AccountPoolCpaExportService(AsyncSessionLocal, account_pool_authorization_service, cpa=cpa)


@router.post("/teams/{team_id}/push-cpa")
async def push_team(team_id: int, *, db=Depends(get_db), service=Depends(get_team_export_service)):
    try:
        result = await service.push(team_id, db)
        return JSONResponse({"success": True, "data": result}, headers=NO_STORE)
    except (MemberAuthorizationError, CpaExportError) as exc:
        await db.rollback()
        return JSONResponse({"success": False, "error": str(exc)}, status_code=400, headers=NO_STORE)
    except Exception:
        await db.rollback()
        logger.exception("Team CPA 导出失败: team=%s", team_id)
        return JSONResponse({"success": False, "error": "CPA 导出失败，请检查服务端日志"},
                            status_code=500, headers=NO_STORE)


@router.post("/account-pool/{entry_id}/export-cpa")
async def push_pool(entry_id: int, payload: CpaExportRequest, background_tasks: BackgroundTasks, *,
                    db=Depends(get_db), service=Depends(get_pool_export_service)):
    try:
        job = await service.enqueue(db, entry_id, payload.workspace_id)
        background_tasks.add_task(service.run, job.id)
        return JSONResponse({"success": True, "job_id": job.id}, status_code=202, headers=NO_STORE)
    except AccountPoolAuthorizationError as exc:
        await db.rollback()
        return JSONResponse({"success": False, "error": str(exc)}, status_code=400, headers=NO_STORE)


@router.get("/account-pool/cpa-export-jobs/{job_id}")
async def pool_export_status(job_id: int, db=Depends(get_db)):
    job = await db.get(CpaExportJob, job_id)
    if job is None:
        raise HTTPException(404, "CPA 导出任务不存在")
    return JSONResponse({"status": job.status, "error": job.error, "filename": job.filename,
                         "workspace_id": job.workspace_id}, headers=NO_STORE)
