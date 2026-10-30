"""Track API Key exports independently from Team/OAuth account exports."""
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.official_key_model import OfficialKey, OfficialKeyExport
from app.services.sub2api import Sub2apiError, Sub2apiImportUncertain
from app.utils.time_utils import get_now


def export_status(record):
    if record is None:
        return None
    return {"status": record.status, "account_id": record.account_id, "error": record.error,
            "exported_at": record.exported_at.isoformat() if record.exported_at else None}


async def statuses(db):
    records = (await db.execute(select(OfficialKeyExport))).scalars().all()
    return {record.official_key_id: export_status(record) for record in records}


class OfficialKeyExportService:
    def __init__(self, *, gateway, cipher):
        self._gateway = gateway
        self._cipher = cipher

    async def _claim(self, db, *, entry_id, base_url):
        record = await db.get(OfficialKeyExport, entry_id)
        if record and record.base_url != base_url:
            raise Sub2apiError("Sub2API 地址已变更，请先核对原站点的官key导出关联")
        if record and record.status in ("exporting", "uncertain"):
            raise Sub2apiError("上次导出仍在进行或结果不确定，请先到 Sub2API 核对，未重复创建")
        if record:
            claimed = await db.execute(update(OfficialKeyExport).where(
                OfficialKeyExport.official_key_id == entry_id,
                OfficialKeyExport.status == record.status,
            ).values(status="exporting", error=None, operation_id=uuid4().hex))
            if not claimed.rowcount:
                raise Sub2apiError("该官key正在导出，请等待结果")
        else:
            db.add(OfficialKeyExport(official_key_id=entry_id, base_url=base_url,
                                    operation_id=uuid4().hex, status="exporting"))
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise Sub2apiError("该官key正在导出，请等待结果") from exc
        return await db.get(OfficialKeyExport, entry_id, populate_existing=True)

    async def push(self, entry_id, db):
        entry = await db.get(OfficialKey, entry_id)
        if entry is None:
            raise HTTPException(404, "官key 记录不存在")
        config = await self._gateway.config(db)
        record = await self._claim(db, entry_id=entry_id, base_url=config.base_url)
        try:
            await db.refresh(entry)
            source = {"account": entry.account,
                      "api_key": self._cipher.decrypt_token(entry.api_key_encrypted)}
            payload, template_id = await self._gateway.payload(db, config=config, entry=source)
            if record.account_id:
                await self._gateway.verify_target(
                    config, account_id=record.account_id, template_id=template_id
                )
            account_id = await self._gateway.write(config, payload=payload, record=record)
        except Sub2apiImportUncertain as exc:
            await self._failure(db, record=record, status="uncertain", error=str(exc))
            raise
        except Sub2apiError as exc:
            await self._failure(db, record=record, status="failed", error=str(exc))
            raise
        record.account_id = account_id
        record.status = "exported"
        record.exported_at = get_now()
        await db.commit()
        return export_status(record)

    async def _failure(self, db, *, record, status, error):
        record.status = status
        record.error = error
        await db.commit()
