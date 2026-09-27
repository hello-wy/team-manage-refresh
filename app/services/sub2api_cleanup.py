"""Durable, workspace-scoped cleanup of exported accounts after membership removal."""
import logging
from datetime import timedelta

from sqlalchemy import or_, select, update

from app.models import (AccountPoolEntry, AccountPoolExportJob, MemberAuthorization,
                        Sub2apiAccountLink, Sub2apiExportRecord)
from app.services.settings import settings_service
from app.services.sub2api import DEFAULT_BASE_URL, normalize_base_url, sub2api_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


async def track_export(db, email, space, account_id):
    """保存每次创建的 ID，避免同一邮箱重复导出时旧 ID 被覆盖。"""
    if type(account_id) is not int or account_id <= 0:
        return None
    base_url = normalize_base_url(await settings_service.get_setting(
        db, "sub2api_base_url", DEFAULT_BASE_URL))
    link = (await db.execute(select(Sub2apiAccountLink).where(
        Sub2apiAccountLink.base_url == base_url,
        Sub2apiAccountLink.sub2api_account_id == account_id,
    ))).scalar_one_or_none()
    if link is None:
        link = Sub2apiAccountLink(base_url=base_url, sub2api_account_id=account_id,
                                 email=email, team_space_id=space, status="active")
        db.add(link)
        await db.flush()
    elif link.email != email or link.team_space_id != space:
        raise ValueError("sub2api 账号 ID 已关联其他邮箱或 Team 空间")
    return link


async def enqueue_member_cleanup(db, team, email):
    """与 confirmed removed 状态一起提交；不扫描并清理历史已离组账号。"""
    email = str(email or "").strip().lower()
    space = str(team.account_id or "").strip()
    if not email or not space or email == str(team.email or "").strip().lower():
        return 0
    ids = set((await db.execute(select(Sub2apiExportRecord.sub2api_account_id).where(
        Sub2apiExportRecord.email == email, Sub2apiExportRecord.team_space_id == space,
    ))).scalars())
    ids.update((await db.execute(select(MemberAuthorization.sub2api_account_id).where(
        MemberAuthorization.email == email, MemberAuthorization.account_id == space,
    ))).scalars())
    ids.update((await db.execute(select(AccountPoolExportJob.sub2api_account_id)
        .join(AccountPoolEntry, AccountPoolEntry.id == AccountPoolExportJob.account_pool_id)
        .where(AccountPoolEntry.email == email, AccountPoolExportJob.workspace_id == space,
               AccountPoolExportJob.status == "completed"))).scalars())
    # 兼容上线前只保存在导出记录/授权/后台任务中的远程 ID。
    known_ids = set((await db.execute(select(Sub2apiAccountLink.sub2api_account_id).where(
        Sub2apiAccountLink.email == email, Sub2apiAccountLink.team_space_id == space,
    ))).scalars())
    for account_id in ids - known_ids:
        await track_export(db, email, space, account_id)
    links = (await db.execute(select(Sub2apiAccountLink).where(
        Sub2apiAccountLink.email == email, Sub2apiAccountLink.team_space_id == space,
        Sub2apiAccountLink.status == "active",
    ))).scalars().all()
    for link in links:
        link.status = "pending"
        link.deletion_requested_at = get_now()
        link.next_attempt_at = get_now()
    return len(links)


async def process_pending_cleanup(db, service=sub2api_service):
    ids = list((await db.execute(select(Sub2apiAccountLink.id).where(
        Sub2apiAccountLink.status == "pending",
        or_(Sub2apiAccountLink.next_attempt_at.is_(None),
            Sub2apiAccountLink.next_attempt_at <= get_now()),
    ).order_by(Sub2apiAccountLink.next_attempt_at, Sub2apiAccountLink.id).limit(50))).scalars())
    stats = {"scanned": len(ids), "deleted": 0, "failed": 0}
    for link_id in ids:
        link = await db.get(Sub2apiAccountLink, link_id, populate_existing=True)
        if link is None or link.status != "pending":
            continue
        email, space, account_id, base_url = (
            link.email, link.team_space_id, link.sub2api_account_id, link.base_url)
        try:
            await service.delete_exported_account(account_id, email, space, base_url, db)
            # 旧删除任务不能清空同邮箱重新入组后新导出的账号标记。
            await db.execute(update(MemberAuthorization).where(
                MemberAuthorization.email == email, MemberAuthorization.account_id == space,
                MemberAuthorization.sub2api_account_id == account_id,
            ).values(sub2api_account_id=None, sub2api_exported_at=None))
            await db.execute(update(Sub2apiExportRecord).where(
                Sub2apiExportRecord.email == email, Sub2apiExportRecord.team_space_id == space,
                Sub2apiExportRecord.sub2api_account_id == account_id,
            ).values(sub2api_account_id=None))
            link.status = "deleted"
            link.deleted_at = get_now()
            link.last_error = None
            link.attempts += 1
            await db.commit()
            stats["deleted"] += 1
            logger.info("退组后删除 sub2api 账号成功: account_id=%s email=%s workspace=%s",
                        account_id, email, space)
        except Exception as exc:
            await db.rollback()
            link = await db.get(Sub2apiAccountLink, link_id)
            link.attempts += 1
            # 不保存外部响应或凭据；已知服务错误只含固定提示。
            from app.services.sub2api import Sub2apiError
            link.last_error = str(exc) if isinstance(exc, Sub2apiError) else type(exc).__name__
            link.next_attempt_at = get_now() + timedelta(minutes=1)
            await db.commit()
            stats["failed"] += 1
            logger.warning("退组后删除 sub2api 账号待重试: account_id=%s reason=%s",
                           account_id, link.last_error)
    return stats
