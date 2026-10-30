"""官key 的持久化与加解密。"""
from typing import Protocol

from fastapi import HTTPException
from sqlalchemy import delete, select, update

from app.official_key_model import OfficialKey, OfficialKeyExport, OfficialKeyExpiration


class KeyCipher(Protocol):
    def encrypt_token(self, token: str) -> str: ...

    def decrypt_token(self, encrypted_token: str) -> str: ...


async def list_keys(db, *, cipher: KeyCipher) -> list[dict]:
    entries = (await db.execute(
        select(OfficialKey).order_by(OfficialKey.id.desc())
    )).scalars().all()
    return [{
        "id": entry.id,
        "account": entry.account,
        "api_key": cipher.decrypt_token(entry.api_key_encrypted),
    } for entry in entries]


async def create_key(db, *, payload, cipher: KeyCipher) -> dict:
    entry = OfficialKey(
        account=payload.account,
        api_key_encrypted=cipher.encrypt_token(payload.api_key),
    )
    db.add(entry)
    await db.commit()
    return {"id": entry.id, "account": payload.account, "api_key": payload.api_key}


async def update_key(db, *, entry_id: int, payload, cipher: KeyCipher) -> dict:
    await require_idle_export(db, entry_id=entry_id)
    result = await db.execute(update(OfficialKey).where(
        OfficialKey.id == entry_id
    ).values(account=payload.account, api_key_encrypted=cipher.encrypt_token(payload.api_key)))
    if not result.rowcount:
        raise HTTPException(404, "官key 记录不存在")
    await db.execute(update(OfficialKeyExport).where(
        OfficialKeyExport.official_key_id == entry_id,
        OfficialKeyExport.status == "exported",
    ).values(status="changed"))
    await db.commit()
    return {"id": entry_id, "account": payload.account, "api_key": payload.api_key}


async def delete_key(db, *, entry_id: int) -> None:
    await require_idle_export(db, entry_id=entry_id)
    result = await db.execute(delete(OfficialKey).where(OfficialKey.id == entry_id))
    if not result.rowcount:
        raise HTTPException(404, "官key 记录不存在")
    await db.execute(delete(OfficialKeyExport).where(OfficialKeyExport.official_key_id == entry_id))
    await db.execute(delete(OfficialKeyExpiration).where(OfficialKeyExpiration.official_key_id == entry_id))
    await db.commit()


async def require_idle_export(db, *, entry_id):
    record = await db.get(OfficialKeyExport, entry_id)
    if record and record.status in ("exporting", "uncertain"):
        raise HTTPException(409, "官key导出仍在进行或结果不确定，请先核对 Sub2API")
