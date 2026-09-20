"""账号号池录入、删除及登录凭据的加密存储。"""
from typing import Optional, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry
from app.services.account_pool_credential_input import (
    AccountPoolCredentialRecord,
    AccountPoolImportResult,
    parse_account_inputs,
    parse_export_names,
)
from app.services.account_pool_history import account_pool_history_service
from app.services.encryption import encryption_service
from app.utils.totp import TotpError, generate_totp, normalize_totp_secret

class CredentialCipher(Protocol):
    def encrypt_token(self, token: str) -> str: ...
    def decrypt_token(self, encrypted_token: str) -> str: ...


class AccountPoolCredentialError(ValueError):
    pass


class AccountPoolCredentialService:
    def __init__(self, cipher: CredentialCipher):
        self._cipher = cipher

    parse_export_names = staticmethod(parse_export_names)
    parse_account_inputs = staticmethod(parse_account_inputs)

    async def add_accounts(
        self,
        db_session: AsyncSession,
        *,
        emails: Optional[list[str]] = None,
        content: str = "",
    ) -> dict[str, object]:
        records, invalid = parse_account_inputs(emails, content)
        if not records:
            return self._empty_add_result(invalid)
        result = await self._persist_records(db_session, records)
        return {
            "success": True,
            "message": self._build_add_message(result, invalid),
            "added": result.added,
            "restored": result.restored,
            "updated": result.updated,
            "existing": result.existing,
            "invalid": invalid,
        }

    async def import_export_names(
        self,
        db_session: AsyncSession,
        *,
        names: list[str],
    ) -> dict[str, object]:
        records, invalid = parse_export_names(names)
        if invalid:
            raise AccountPoolCredentialError(f"存在 {len(invalid)} 条无效账号名称")
        if not records:
            raise AccountPoolCredentialError("未发现可导入的账号凭据")
        result = await self._persist_records(db_session, records)
        return {
            "added": result.added,
            "restored": result.restored,
            "updated": result.updated,
            "total": len(records),
        }

    async def _persist_records(
        self,
        db_session: AsyncSession,
        records: list[AccountPoolCredentialRecord],
    ) -> AccountPoolImportResult:
        entries = await self._entries_by_email(db_session, records)
        result = self._apply_records(db_session, entries, records)
        await db_session.flush()
        await account_pool_history_service.backfill_current_histories(
            db_session,
            [entries[record.email] for record in records],
        )
        await db_session.commit()
        return result

    @staticmethod
    async def _entries_by_email(
        db_session: AsyncSession,
        records: list[AccountPoolCredentialRecord],
    ) -> dict[str, AccountPoolEntry]:
        emails = [record.email for record in records]
        result = await db_session.execute(
            select(AccountPoolEntry).where(AccountPoolEntry.email.in_(emails))
        )
        return {entry.email: entry for entry in result.scalars().all()}

    def _apply_records(
        self,
        db_session: AsyncSession,
        entries: dict[str, AccountPoolEntry],
        records: list[AccountPoolCredentialRecord],
    ) -> AccountPoolImportResult:
        added: list[str] = []
        restored: list[str] = []
        updated: list[str] = []
        existing: list[str] = []
        for record in records:
            entry = entries.get(record.email)
            if entry is None:
                entry = AccountPoolEntry(email=record.email)
                db_session.add(entry)
                entries[record.email] = entry
                added.append(record.email)
            elif entry.deleted_at is not None:
                entry.deleted_at = None
                entry.liveness_status = None
                entry.liveness_checked_at = None
                entry.liveness_message = None
                restored.append(record.email)
            elif record.has_credentials:
                updated.append(record.email)
            else:
                existing.append(record.email)
            if record.has_credentials:
                entry.password_encrypted = self._cipher.encrypt_token(record.password or "")
                entry.two_factor_secret_encrypted = self._cipher.encrypt_token(
                    record.two_factor_secret or ""
                )
                entry.liveness_status = None
                entry.liveness_checked_at = None
                entry.liveness_message = None
        return AccountPoolImportResult(added, restored, updated, existing)

    async def get_credentials(
        self,
        db_session: AsyncSession,
        email: str,
    ) -> dict[str, str] | None:
        normalized = str(email or "").strip().lower()
        result = await db_session.execute(
            select(AccountPoolEntry).where(
                AccountPoolEntry.email == normalized,
                AccountPoolEntry.deleted_at.is_(None),
            )
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            return None
        return {
            "email": entry.email,
            "password": self._decrypt(entry.password_encrypted),
            "two_factor_secret": self._decrypt(entry.two_factor_secret_encrypted),
        }

    async def update_credentials(
        self,
        db_session: AsyncSession,
        entry_id: int,
        *,
        password: Optional[str] = None,
        two_factor_secret: Optional[str] = None,
    ) -> Optional[dict[str, str]]:
        entry = await db_session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            return None
        if password is None and two_factor_secret is None:
            raise AccountPoolCredentialError("请至少修改密码或 2FA")
        encrypted_password = None
        encrypted_secret = None
        if password is not None:
            if not password:
                raise AccountPoolCredentialError("密码不能为空")
            encrypted_password = self._cipher.encrypt_token(password)
        if two_factor_secret is not None:
            normalized_secret = self._normalize_valid_secret(two_factor_secret)
            encrypted_secret = self._cipher.encrypt_token(normalized_secret)
        if encrypted_password is not None:
            entry.password_encrypted = encrypted_password
        if encrypted_secret is not None:
            entry.two_factor_secret_encrypted = encrypted_secret
        entry.liveness_status = None
        entry.liveness_checked_at = None
        entry.liveness_message = None
        await db_session.commit()
        return {
            "email": entry.email,
            "password": self._decrypt(entry.password_encrypted),
            "two_factor_secret": self._decrypt(entry.two_factor_secret_encrypted),
        }

    @staticmethod
    def _normalize_valid_secret(value: str) -> str:
        normalized = normalize_totp_secret(value)
        try:
            generate_totp(normalized, timestamp=0)
        except TotpError as exc:
            raise AccountPoolCredentialError(str(exc)) from exc
        return normalized

    def _decrypt(self, encrypted_value: str | None) -> str:
        if not encrypted_value:
            return ""
        try:
            return self._cipher.decrypt_token(encrypted_value)
        except Exception as exc:
            raise AccountPoolCredentialError("账号凭据解密失败，请检查加密密钥") from exc

    @staticmethod
    async def delete_entry(db_session: AsyncSession, entry_id: int) -> bool:
        entry = await db_session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            return False
        await db_session.delete(entry)
        await db_session.commit()
        return True

    @staticmethod
    def _empty_add_result(invalid: list[str]) -> dict[str, object]:
        return {
            "success": False,
            "message": "未发现可添加的有效账号",
            "added": [],
            "restored": [],
            "updated": [],
            "existing": [],
            "invalid": invalid,
        }

    @staticmethod
    def _build_add_message(
        result: AccountPoolImportResult,
        invalid: list[str],
    ) -> str:
        parts = [
            f"新增 {len(result.added)} 个账号",
            f"恢复 {len(result.restored)} 个账号",
            f"更新凭据 {len(result.updated)} 个",
            f"已存在 {len(result.existing)} 个",
        ]
        if invalid:
            parts.append(f"无效 {len(invalid)} 条")
        return "，".join(parts)


account_pool_credential_service = AccountPoolCredentialService(encryption_service)
