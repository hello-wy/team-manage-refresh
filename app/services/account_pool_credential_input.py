"""账号号池输入格式解析。"""
import re
from dataclasses import dataclass
from typing import Optional

ACCOUNT_NAME_SEPARATOR = "----"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class AccountPoolCredentialRecord:
    email: str
    password: Optional[str] = None
    two_factor_secret: Optional[str] = None

    @property
    def has_credentials(self) -> bool:
        return self.password is not None and self.two_factor_secret is not None


@dataclass(frozen=True)
class AccountPoolImportResult:
    added: list[str]
    restored: list[str]
    updated: list[str]
    existing: list[str]


def parse_export_names(
    names: list[str],
) -> tuple[list[AccountPoolCredentialRecord], list[str]]:
    records: list[AccountPoolCredentialRecord] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        raw = str(raw_name or "")
        record = _parse_credential_input(raw)
        if record is None or record.email in seen:
            invalid.append(raw)
            continue
        seen.add(record.email)
        records.append(record)
    return records, invalid


def parse_account_inputs(
    emails: Optional[list[str]] = None,
    content: str = "",
) -> tuple[list[AccountPoolCredentialRecord], list[str]]:
    values = [*list(emails or []), *content.splitlines()]
    records: dict[str, AccountPoolCredentialRecord] = {}
    invalid: list[str] = []
    for raw_value in values:
        raw = str(raw_value or "").strip()
        if not raw:
            continue
        record = _parse_account_input(raw)
        if record is None:
            invalid.append(raw)
            continue
        previous = records.get(record.email)
        if previous is None or record.has_credentials:
            records[record.email] = record
    return list(records.values()), invalid


def _parse_account_input(raw: str) -> Optional[AccountPoolCredentialRecord]:
    parts = [part.strip() for part in raw.split(ACCOUNT_NAME_SEPARATOR)]
    if len(parts) == 1:
        email = parts[0].lower()
        return AccountPoolCredentialRecord(email) if EMAIL_PATTERN.fullmatch(email) else None
    return _record_from_parts(parts)


def _parse_credential_input(raw: str) -> Optional[AccountPoolCredentialRecord]:
    parts = [part.strip() for part in raw.split(ACCOUNT_NAME_SEPARATOR)]
    return _record_from_parts(parts)


def _record_from_parts(parts: list[str]) -> Optional[AccountPoolCredentialRecord]:
    if len(parts) != 3 or not all(parts):
        return None
    email, password, two_factor_secret = parts
    normalized_email = email.lower()
    if not EMAIL_PATTERN.fullmatch(normalized_email):
        return None
    return AccountPoolCredentialRecord(normalized_email, password, two_factor_secret)
