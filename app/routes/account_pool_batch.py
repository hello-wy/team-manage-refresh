"""Admin batch operations for selected account-pool entries."""
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_db
from app.dependencies.auth import require_admin
from app.routes.account_pool_totp import totp_service
from app.routes.admin import account_pool_export_service
from app.services.account_pool_batch import create_account_pool_batch_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/account-pool/batch", tags=["admin-account-pool"])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
HISTORY_PAGE_SIZE = 20
batch_service = create_account_pool_batch_service(
    AsyncSessionLocal, totp_service
)


class BatchSelection(BaseModel):
    ids: list[int] = Field(min_length=1)


def batch_error(exc):
    return JSONResponse(status_code=400, content={"success": False, "error": str(exc)},
                        headers=NO_STORE)


@router.post("/export-json")
async def batch_export_json(
    payload: BatchSelection,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        merged = await batch_service.merged_json(db, payload.ids)
    except (ValueError, KeyError, TypeError) as exc:
        return batch_error(exc)
    except Exception:
        logger.exception("账号池批量 JSON 导出失败")
        return batch_error(ValueError("合并 JSON 失败，请检查服务端日志"))
    return Response(content=json.dumps(merged, ensure_ascii=False, indent=2),
                    media_type="application/json", headers={
                        **NO_STORE,
                        "Content-Disposition": 'attachment; filename="sub2api-account-pool-batch.json"',
                    })


@router.post("/sub2api")
async def batch_export_sub2api(
    payload: BatchSelection,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        jobs = await batch_service.enqueue_exports(db, payload.ids)
    except ValueError as exc:
        await db.rollback()
        return batch_error(exc)
    background_tasks.add_task(run_exports, [job.id for job in jobs])
    return JSONResponse(status_code=202, content={"success": True,
        "job_ids": [job.id for job in jobs]}, headers=NO_STORE)


async def run_exports(job_ids):
    for job_id in job_ids:
        await account_pool_export_service.run(job_id)


@router.post("/rotate-2fa")
async def batch_rotate_totp(
    payload: BatchSelection,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        batch_id = await batch_service.enqueue_rotations(db, payload.ids)
    except ValueError as exc:
        await db.rollback()
        return batch_error(exc)
    background_tasks.add_task(batch_service.run_rotations, batch_id)
    return JSONResponse(status_code=202, content={"success": True,
        "batch_id": batch_id}, headers=NO_STORE)


@router.get("/rotate-2fa/{batch_id}")
async def batch_rotate_status(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        results = await batch_service.rotation_status(db, batch_id)
    except ValueError as exc:
        return batch_error(exc)
    return JSONResponse(content={"success": True, "results": results}, headers=NO_STORE)


@router.get("/rotate-2fa-history")
async def batch_rotate_history(
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    total, results = await batch_service.rotation_history(db, page, HISTORY_PAGE_SIZE)
    return JSONResponse(content={"success": True, "results": results,
                                 "total": total, "page": page,
                                 "page_size": HISTORY_PAGE_SIZE}, headers=NO_STORE)


@router.post("/delete")
async def batch_delete(
    payload: BatchSelection,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        emails = await batch_service.delete(db, payload.ids)
    except ValueError as exc:
        await db.rollback()
        return batch_error(exc)
    logger.info("账号池批量删除: emails=%s result=deleted", ", ".join(emails))
    return JSONResponse(content={"success": True, "deleted": emails}, headers=NO_STORE)
