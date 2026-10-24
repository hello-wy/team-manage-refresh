"""Contracts shared by account-pool authorization consumers."""
from dataclasses import dataclass
from typing import Any


class AccountPoolAuthorizationError(ValueError):
    pass


@dataclass(frozen=True)
class AccountPoolLoginResult:
    payload: dict[str, Any]
    workspace: dict[str, Any]
    verified_totp_factor_id: str = ""
    phone_number: str = ""
    phone_rental_error: str = ""


