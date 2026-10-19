"""Populate names only as part of importing new or restored pool accounts."""
import logging

from sqlalchemy import select

from app.models import AccountPoolEntry

logger = logging.getLogger(__name__)


class AccountPoolImportService:
    def __init__(self, authorization, rotations, save_name):
        self._authorization = authorization
        self._rotations = rotations
        self._save_name = save_name

    async def complete(self, db, result, *, background_tasks, rotate_2fa):
        emails = [*result.get("added", []), *result.get("restored", [])]
        if not result["success"]:
            return result
        if not rotate_2fa:
            profiles = await self._populate_names(db, emails)
            return {**result, "profiles": profiles}
        try:
            rotation = await self._rotations.enqueue_import_rotations(db, emails)
        except Exception:
            await db.rollback()
            logger.exception("账号已加入号池，但导入 2FA 与补姓名任务创建失败")
            return {**result, "rotation_error": "账号已加入号池，但 2FA 与补姓名任务创建失败"}
        if rotation["batch_id"]:
            background_tasks.add_task(self._rotations.run_rotations, rotation["batch_id"],
                                      populate_names=True)
        owners = [item["email"] for item in rotation["skipped"]
                  if item["reason"] == "Team 所有者不自动更换 2FA"]
        profiles = await self._populate_names(db, owners)
        return {**result, "rotation": rotation, "profiles": profiles}

    async def _populate_names(self, db, emails):
        profiles = {"saved": [], "skipped": [], "failed": []}
        if not emails:
            return profiles
        entries = (await db.execute(select(
            AccountPoolEntry.id, AccountPoolEntry.email, AccountPoolEntry.name,
            AccountPoolEntry.password_encrypted, AccountPoolEntry.two_factor_secret_encrypted,
        ).where(
            AccountPoolEntry.email.in_(emails), AccountPoolEntry.deleted_at.is_(None),
        ))).all()
        for entry in entries:
            if entry.name is not None:
                continue
            if not entry.password_encrypted or not entry.two_factor_secret_encrypted:
                profiles["skipped"].append({"email": entry.email, "reason": "缺少登录密码或 2FA 密钥"})
                continue
            error = await self._populate_entry(db, entry.id, email=entry.email)
            if error:
                profiles["failed"].append({"email": entry.email, "error": error})
            else:
                profiles["saved"].append(entry.email)
        return profiles

    async def _populate_entry(self, db, entry_id, *, email):
        try:
            login = await self._authorization.login_entry(db, entry_id)
            await self._save_name(db, entry_id, login.payload)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.exception("导入账号补姓名失败: entry=%s email=%s", entry_id, email)
            return str(exc)
        return None
