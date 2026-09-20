"""账号号池登录凭据的解析、加密存储与读取。"""
import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountPoolEntry, AccountPoolHistory
from app.services.encryption import encryption_service

ACCOUNT_NAME_SEPARATOR = "----"
ACCOUNT_POOL_SEAT_TYPES = {"default", "premium"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CredentialCipher(Protocol):
    def encrypt_token(self, token: str) -> str: ...
    def decrypt_token(self, encrypted_token: str) -> str: ...


@dataclass(frozen=True)
class AccountPoolCredentialRecord:
    email: str
    password: str
    two_factor_secret: str


class AccountPoolCredentialError(ValueError):
    pass


class AccountPoolCredentialService:
    def __init__(self, cipher: CredentialCipher):
        self._cipher = cipher

    @staticmethod
    def parse_export_names(names: list[str]) -> tuple[list[AccountPoolCredentialRecord], list[str]]:
        records: list[AccountPoolCredentialRecord] = []
        invalid: list[str] = []
        seen: set[str] = set()
        for raw_name in names:
            parts = [part.strip() for part in str(raw_name or "").split(ACCOUNT_NAME_SEPARATOR)]
            if len(parts) != 3 or not all(parts):
                invalid.append(str(raw_name or ""))
                continue
            email, password, two_factor_secret = parts
            email = email.lower()
            if not EMAIL_PATTERN.fullmatch(email) or email in seen:
                invalid.append(str(raw_name or ""))
                continue
            seen.add(email)
            records.append(AccountPoolCredentialRecord(email, password, two_factor_secret))
        return records, invalid

    async def import_export_names(
        self,
        db_session: AsyncSession,
        *,
        names: list[str],
        seat_type: str = "default",
    ) -> dict[str, object]:
        if seat_type not in ACCOUNT_POOL_SEAT_TYPES:
            raise AccountPoolCredentialError("不支持的席位类型")
        records, invalid = self.parse_export_names(names)
        if invalid:
            raise AccountPoolCredentialError(f"存在 {len(invalid)} 条无效账号名称")
        if not records:
            raise AccountPoolCredentialError("未发现可导入的账号凭据")
        entries = await self._entries_by_email(db_session, records)
        added, updated = self._apply_records(db_session, entries, records, seat_type)
        await db_session.commit()
        return {"added": added, "updated": updated, "total": len(records)}

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
        seat_type: str,
    ) -> tuple[list[str], list[str]]:
        added: list[str] = []
        updated: list[str] = []
        for record in records:
            entry = entries.get(record.email)
            if entry is None:
                entry = AccountPoolEntry(email=record.email, seat_type=seat_type)
                db_session.add(entry)
                added.append(record.email)
            else:
                entry.seat_type = seat_type
                updated.append(record.email)
            entry.password_encrypted = self._cipher.encrypt_token(record.password)
            entry.two_factor_secret_encrypted = self._cipher.encrypt_token(record.two_factor_secret)
        return added, updated

    async def get_credentials(
        self,
        db_session: AsyncSession,
        email: str,
    ) -> dict[str, str] | None:
        normalized = str(email or "").strip().lower()
        result = await db_session.execute(
            select(AccountPoolEntry).where(AccountPoolEntry.email == normalized)
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            return None
        return {
            "email": entry.email,
            "password": self._decrypt(entry.password_encrypted),
            "two_factor_secret": self._decrypt(entry.two_factor_secret_encrypted),
        }

    def _decrypt(self, encrypted_value: str | None) -> str:
        if not encrypted_value:
            return ""
        try:
            return self._cipher.decrypt_token(encrypted_value)
        except Exception as exc:
            raise AccountPoolCredentialError("账号凭据解密失败，请检查加密密钥") from exc

    @staticmethod
    async def delete_entry(db_session: AsyncSession, entry_id: int) -> bool:
        exists = await db_session.get(AccountPoolEntry, entry_id)
        if exists is None:
            return False
        await db_session.execute(
            delete(AccountPoolHistory).where(AccountPoolHistory.account_pool_id == entry_id)
        )
        await db_session.execute(delete(AccountPoolEntry).where(AccountPoolEntry.id == entry_id))
        await db_session.commit()
        return True


account_pool_credential_service = AccountPoolCredentialService(encryption_service)
