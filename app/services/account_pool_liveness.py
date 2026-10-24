"""Verify a single account without logging in to every workspace or exporting JSON."""
import asyncio
import logging
import time

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AccountPoolEntry, Team
from app.services.account_pool_owner_liveness import AccountPoolOwnerLivenessService
from app.services.account_pool_authorization import (
    AccountPoolAuthorizationError,
    AccountPoolAuthorizationService,
)
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)
INVALID_ACCOUNT_CODES = frozenset({"invalid_username_or_password", "account_deactivated"})
CHECK_TIMEOUT_SECONDS = 60


class LivenessCheckInProgressError(ValueError):
    pass


class AccountPoolLivenessService:
    def __init__(self, credential_service, authorization_service: AccountPoolAuthorizationService, team_service=None,
                 *, timeout_seconds=CHECK_TIMEOUT_SECONDS):
        self._credentials = credential_service
        self._authorization = authorization_service
        self._timeout_seconds = timeout_seconds
        self._owners = AccountPoolOwnerLivenessService(team_service, timeout_seconds=timeout_seconds) if team_service else None
        self._checking = set()

    async def check_entry(
        self, session: AsyncSession, entry_id: int, *, refresh_team: bool = False,
    ) -> str | dict | None:
        model = Team if entry_id < 0 else AccountPoolEntry
        record = await session.get(model, abs(entry_id))
        if record is None or (entry_id > 0 and record.deleted_at is not None):
            return None
        email = record.email.strip().lower()
        if email in self._checking:
            raise LivenessCheckInProgressError("该账号正在验活，请等待本次检查完成")
        self._checking.add(email)
        try:
            return await self._check_entry(session, entry_id, refresh_team=refresh_team)
        finally:
            self._checking.discard(email)

    async def _check_entry(
        self, session: AsyncSession, entry_id: int, *, refresh_team: bool = False,
    ) -> str | dict | None:
        if entry_id < 0:
            return await self._owners.check(session, -entry_id) if self._owners else None
        entry = await session.get(AccountPoolEntry, entry_id)
        if entry is None or entry.deleted_at is not None:
            return None
        version = (entry.password_encrypted, entry.two_factor_secret_encrypted)
        started = time.monotonic()
        try:
            credentials = await self._credentials.get_credentials(session, entry.email)
            logger.info("账号验活阶段完成: email=%s stage=credentials elapsed=%.3fs",
                        entry.email, time.monotonic() - started)
            if not credentials or not credentials["password"]:
                if self._owners:
                    owner_team_id = (await session.execute(select(Team.id).where(
                        func.lower(func.trim(Team.email)) == entry.email.strip().lower(),
                    ).order_by(Team.id))).scalars().first()
                    if owner_team_id:
                        return await self._owners.check(session, owner_team_id)
                return await self._save_status(session, entry_id, version, "missing", "未保存登录密码")
            scan = await asyncio.wait_for(
                self._authorization.scan_entry(session, entry_id, identifier=f"account-pool-liveness-{entry_id}"),
                timeout=self._timeout_seconds,
            )
            logger.info("账号验活阶段完成: email=%s stage=oauth elapsed=%.3fs",
                        entry.email, time.monotonic() - started)
            status = await self._save_status(session, entry_id, version, "alive", "密码、2FA 与 OAuth 登录验证通过")
            if status is None:
                return None
            if refresh_team:
                try:
                    workspace = await self._authorization.refresh_team_list(session, entry_id, scan=scan)
                    logger.info("账号验活阶段完成: email=%s stage=team-sync elapsed=%.3fs",
                                entry.email, time.monotonic() - started)
                    return {"status": status, "workspace": workspace, "workspace_error": ""}
                except AccountPoolAuthorizationError as exc:
                    return {"status": status, "workspace": None, "workspace_error": str(exc)}
                except Exception as exc:
                    logger.exception("账号 Team 刷新异常: entry_id=%s", entry_id)
                    return {"status": status, "workspace": None, "workspace_error": f"刷新 Team 失败：{exc}"}
            return status
        except asyncio.TimeoutError:
            return await self._save_status(session, entry_id, version, "error", "验活超时，请检查网络或代理后重试")
        except AccountPoolAuthorizationError as exc:
            message = str(exc)
            status = "invalid" if any(code in message for code in INVALID_ACCOUNT_CODES) else "error"
            return await self._save_status(session, entry_id, version, status, message)
        except Exception:
            logger.exception("账号验活异常: entry_id=%s", entry_id)
            return await self._save_status(
                session,
                entry_id,
                version,
                "error",
                "检测请求异常，请查看服务端日志",
            )

    async def _save_status(
        self,
        session: AsyncSession,
        entry_id: int,
        version: tuple[str | None, str | None],
        status: str,
        message: str,
    ) -> str | None:
        now = get_now()
        saved = await session.execute(
            update(AccountPoolEntry)
            .where(
                AccountPoolEntry.id == entry_id,
                AccountPoolEntry.deleted_at.is_(None),
                AccountPoolEntry.password_encrypted == version[0],
                AccountPoolEntry.two_factor_secret_encrypted == version[1],
            )
            .values(
                liveness_status=status,
                liveness_message=message,
                liveness_checked_at=now,
            )
        )
        await session.commit()
        return status if saved.rowcount else None

    async def check_all(self, sessions: async_sessionmaker) -> dict[str, int]:
        async with sessions() as session:
            entry_ids = list((await session.scalars(
                select(AccountPoolEntry.id)
                .where(AccountPoolEntry.deleted_at.is_(None))
                .order_by(AccountPoolEntry.id)
            )).all())
            if self._owners:
                owner_ids = (await session.execute(select(func.min(Team.id)).where(
                    ~select(AccountPoolEntry.id).where(
                        AccountPoolEntry.deleted_at.is_(None),
                        func.lower(func.trim(AccountPoolEntry.email)) == func.lower(func.trim(Team.email)),
                    ).exists(),
                ).group_by(func.lower(func.trim(Team.email))))).scalars()
                entry_ids.extend(-team_id for team_id in owner_ids)
        counts = {"alive": 0, "invalid": 0, "error": 0, "missing": 0}
        for entry_id in entry_ids:
            async with sessions() as session:
                try:
                    status = await self.check_entry(session, entry_id)
                except LivenessCheckInProgressError:
                    continue
            if status is not None:
                counts[status] += 1
        return counts
