"""Scheduled password/TOTP verification for account-pool entries."""
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AccountPoolEntry
from app.services.member_authorization import CLIENT_ID, REDIRECT_URI
from app.services.openai_automatic_login import (
    AutomaticLoginRequest,
    OpenAIAutomaticLoginError,
)
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)
INVALID_ACCOUNT_CODES = frozenset({"invalid_username_or_password", "account_deactivated"})


class AccountPoolLivenessService:
    def __init__(self, credential_service, login_service, auth_client):
        self._credentials = credential_service
        self._login = login_service
        self._auth_client = auth_client

    async def check_entry(self, session: AsyncSession, entry_id: int) -> str | None:
        entry = await session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            return None
        credential_version = (entry.password_encrypted, entry.two_factor_secret_encrypted)
        try:
            credentials = await self._credentials.get_credentials(session, entry.email)
            if not credentials or not credentials["password"]:
                result = ("missing", "未保存登录密码")
            else:
                await self._verify(session, entry_id, credentials)
                result = ("alive", "密码及所需 2FA 验证通过")
        except OpenAIAutomaticLoginError as exc:
            message = str(exc)
            status = "invalid" if any(code in message for code in INVALID_ACCOUNT_CODES) else "error"
            result = (status, message)
        except Exception:
            logger.exception("账号验活异常: entry_id=%s", entry_id)
            result = ("error", "检测请求异常，请查看服务端日志")
        saved = await session.execute(
            update(AccountPoolEntry)
            .where(
                AccountPoolEntry.id == entry_id,
                AccountPoolEntry.deleted_at.is_(None),
                AccountPoolEntry.password_encrypted == credential_version[0],
                AccountPoolEntry.two_factor_secret_encrypted == credential_version[1],
            )
            .values(liveness_status=result[0], liveness_message=result[1], liveness_checked_at=get_now())
        )
        await session.commit()
        return result[0] if saved.rowcount else None

    async def _verify(self, session, entry_id, credentials):
        draft = self._auth_client.create_oauth_authorize_url(CLIENT_ID, REDIRECT_URI)
        draft.update({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI})
        request = AutomaticLoginRequest(
            email=credentials["email"],
            password=credentials["password"],
            totp_secret=credentials["two_factor_secret"],
            account_id="",
            oauth_draft=draft,
            db_session=session,
            identifier=f"account-pool-liveness-{entry_id}",
        )
        await self._login.verify_credentials(request)

    async def check_all(self, sessions: async_sessionmaker) -> dict[str, int]:
        async with sessions() as session:
            entry_ids = list((await session.scalars(
                select(AccountPoolEntry.id)
                .where(AccountPoolEntry.deleted_at.is_(None))
                .order_by(AccountPoolEntry.id)
            )).all())
        counts = {"alive": 0, "invalid": 0, "error": 0, "missing": 0}
        for entry_id in entry_ids:
            async with sessions() as session:
                status = await self.check_entry(session, entry_id)
            if status is not None:
                counts[status] += 1
        return counts
