"""Data contracts for the OpenAI automatic login flow."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.openai_sentinel import issue_sentinel_token


class OpenAIAutomaticLoginError(ValueError):
    pass


@dataclass(frozen=True)
class AutomaticLoginDependencies:
    get_session: Callable[..., Awaitable[Any]]
    clear_session: Callable[[str], Awaitable[None]]
    exchange_code: Callable[..., Awaitable[dict[str, Any]]]
    issue_sentinel: Callable[[Any, str], Awaitable[str]] = issue_sentinel_token


@dataclass(frozen=True)
class AutomaticLoginRequest:
    email: str
    password: str
    totp_secret: str
    account_id: str
    oauth_draft: dict[str, str]
    db_session: Any
    identifier: str
