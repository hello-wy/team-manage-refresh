"""Refresh workspace-scoped quota observations independently of page views."""

import json

from sqlalchemy import select

from app.models import (AccountPoolEntry, AccountPoolHistory, AccountPoolWorkspace, QuotaSnapshot,
                        RotationSeatSnapshot, Sub2apiExportRecord, Team, TeamEmailMapping)
from app.services.account_pool_team_usage import record_quota_observation
from app.services.encryption import encryption_service
from app.utils.seats import normalize_seat_type
from app.utils.time_utils import get_now


def _window_values(window):
    if not window:
        return None, None, None
    return window.get("remaining"), window.get("limit"), window.get("reset_at")


def _quota_json(usage, observed_at):
    return {"status": usage["status"], "error": usage.get("error"),
            "observed_at": observed_at.isoformat(),
            "5h": usage.get("5h"), "1week": usage.get("1week")}


def _updated_json(encrypted, space_id, quota):
    payload = json.loads(encryption_service.decrypt_token(encrypted))
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError("账号池 JSON 缺少 accounts 数组")
    matched = False
    for account in accounts:
        if not isinstance(account, dict):
            raise ValueError("账号池 JSON 的账户格式错误")
        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            raise ValueError("账号池 JSON 缺少账户凭据")
        account_id = str(credentials.get("chatgpt_account_id")
                         or credentials.get("account_id") or "").strip()
        if account_id == space_id:
            account["quota"] = quota
            matched = True
    if not matched:
        raise ValueError("账号池 JSON 与额度工作区不匹配")
    return encryption_service.encrypt_token(json.dumps(payload, ensure_ascii=False))


async def _save_account_json_quota(db, email, space_id, quota, observed_at):
    entry = (await db.execute(select(AccountPoolEntry).where(
        AccountPoolEntry.email == email, AccountPoolEntry.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if entry is None:
        return
    workspace = (await db.execute(select(AccountPoolWorkspace).where(
        AccountPoolWorkspace.account_pool_id == entry.id,
        AccountPoolWorkspace.workspace_id == space_id,
    ))).scalar_one_or_none()
    if workspace and workspace.export_json_encrypted:
        workspace.export_json_encrypted = _updated_json(
            workspace.export_json_encrypted, space_id, quota)
        workspace.json_updated_at = observed_at
    if entry.workspace_id == space_id and entry.export_json_encrypted:
        entry.export_json_encrypted = _updated_json(
            entry.export_json_encrypted, space_id, quota)
        entry.export_json_updated_at = observed_at


async def store_quota(db, *, email, space_id, seat_type, usage):
    observed_at = get_now()
    snapshot = (await db.execute(select(QuotaSnapshot).where(
        QuotaSnapshot.email == email, QuotaSnapshot.team_space_id == space_id,
    ))).scalar_one_or_none()
    if snapshot is None:
        snapshot = QuotaSnapshot(email=email, team_space_id=space_id, status="unknown")
        db.add(snapshot)
    short = _window_values(usage.get("5h"))
    weekly = _window_values(usage.get("1week"))
    snapshot.observed_seat_type = seat_type
    snapshot.short_remaining, snapshot.short_limit, snapshot.short_reset_at = short
    snapshot.weekly_remaining, snapshot.weekly_limit, snapshot.weekly_reset_at = weekly
    snapshot.status = usage["status"]
    snapshot.error = usage.get("error")
    snapshot.observed_at = observed_at
    snapshot.version = (snapshot.version or 0) + 1
    await _save_account_json_quota(
        db, email, space_id, _quota_json(usage, observed_at), observed_at)
    await record_quota_observation(
        db,
        email=email,
        team_space_id=space_id,
        seat_type=seat_type,
        usage=usage,
    )
    return snapshot


async def refresh_team(db, team: Team, team_service, *, usage_service):
    members = await team_service.get_team_members(team.id, db)
    balance = members.get("seat_balance") or {}
    if not members.get("success") or not balance.get("success") or not all(
        (balance.get("balance") or {}).get(kind, {}).get("known")
        for kind in ("standard", "premium")
    ):
        raise RuntimeError(members.get("error") or "成员列表或席位余额不完整")
    mappings = (await db.execute(select(TeamEmailMapping).where(
        TeamEmailMapping.team_id == team.id,
        TeamEmailMapping.status == "joined",
    ))).scalars().all()
    owner_email = team.email.strip().lower()
    owner_entry = (await db.execute(select(AccountPoolEntry.id).where(
        AccountPoolEntry.email == owner_email,
        AccountPoolEntry.deleted_at.is_(None),
    ))).scalar_one_or_none() if owner_email else None
    seat_snapshot = await db.get(RotationSeatSnapshot, team.id)
    if seat_snapshot is None:
        seat_snapshot = RotationSeatSnapshot(team_id=team.id)
        db.add(seat_snapshot)
    standard = balance["balance"]["standard"]
    premium = balance["balance"]["premium"]
    seat_snapshot.standard_paid = standard["paid"]
    seat_snapshot.standard_remaining = standard["remaining"]
    seat_snapshot.premium_paid = premium["paid"]
    seat_snapshot.premium_remaining = premium["remaining"]
    seat_snapshot.observed_at = get_now()
    keys = [(row.email, team.account_id) for row in mappings]
    if owner_entry and owner_email not in {row.email for row in mappings}:
        keys.append((owner_email, team.account_id))
    usage_by_key = await usage_service.check_many(db, keys)
    for mapping in mappings:
        usage = usage_by_key[(mapping.email, team.account_id)]
        await store_quota(db, email=mapping.email, space_id=team.account_id,
                          seat_type=normalize_seat_type(mapping.seat_type), usage=usage)
    if (owner_email, team.account_id) in keys and owner_email not in {row.email for row in mappings}:
        await store_quota(db, email=owner_email, space_id=team.account_id,
                          seat_type="unknown", usage=usage_by_key[(owner_email, team.account_id)])
    await db.commit()
    return len(mappings)


async def refresh_export_snapshots(db, usage_service):
    records = (await db.execute(select(Sub2apiExportRecord))).scalars().all()
    enabled = set((await db.execute(select(Team.id).where(
        Team.rotation_mode != "off"))).scalars().all())
    by_key = {(row.email, row.team_space_id): row.seat_type or "unknown"
              for row in records if row.team_id not in enabled}
    keys = list(by_key)
    if not keys:
        return 0
    usages = await usage_service.check_many(db, keys)
    for email, space_id in keys:
        await store_quota(db, email=email, space_id=space_id,
                          seat_type=normalize_seat_type(by_key[(email, space_id)]),
                          usage=usages[(email, space_id)])
    await db.commit()
    return len(keys)


async def refresh_historical_candidates(db, usage_service):
    active = select(TeamEmailMapping.id).where(
        TeamEmailMapping.email == AccountPoolEntry.email,
        TeamEmailMapping.status.in_(("joined", "invited")),
    )
    rows = (await db.execute(select(AccountPoolEntry.email, Team.account_id).join(
        AccountPoolHistory, AccountPoolHistory.account_pool_id == AccountPoolEntry.id,
    ).join(Team, Team.id == AccountPoolHistory.team_id).where(
        Team.rotation_mode != "off", AccountPoolHistory.left_at.is_not(None),
        AccountPoolEntry.deleted_at.is_(None), ~active.exists(),
    ).distinct())).all()
    keys = [(email, space_id) for email, space_id in rows if space_id]
    if not keys:
        return 0
    usages = await usage_service.check_many(db, keys)
    for email, space_id in keys:
        await store_quota(db, email=email, space_id=space_id,
                          seat_type="unknown", usage=usages[(email, space_id)])
    await db.commit()
    return len(keys)
