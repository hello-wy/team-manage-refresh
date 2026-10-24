"""Refresh displayed quota windows and persist the same results for page reloads."""
from sqlalchemy import func, select

from app.models import AccountPoolEntry, AccountPoolTeamUsage, Team
from app.services.account_pool_listing import team_usage_view
from app.services.quota_sync import store_quota
from app.services.rotation_read_model import snapshot_usage


def _quota_key(entry):
    return (entry["email"], entry.get("quota_workspace_id", entry.get("workspace_id")) or "")


async def _usage_details(db, entries):
    rows = (await db.execute(select(AccountPoolEntry.email, AccountPoolTeamUsage).join(
        AccountPoolEntry, AccountPoolEntry.id == AccountPoolTeamUsage.account_pool_id,
    ).where(AccountPoolEntry.email.in_({entry["email"] for entry in entries})))).all()
    return {(email, usage.team_space_id): team_usage_view(usage) for email, usage in rows}


async def refresh_pool_quotas(db, entries, *, usage_service, authorization_service=None):
    eligible = [entry for entry in entries if entry.get("status") == "joined"]
    usages = await usage_service.check_many(
        db, [_quota_key(entry) for entry in eligible]
    ) if eligible else {}
    if authorization_service:
        for entry in eligible:
            key = _quota_key(entry)
            email, space_id = key
            if not space_id or usages.get(key, {}).get("status") != "invalid":
                continue
            # Team owners use the Team token and must not be re-authenticated as pool members.
            is_owner = (await db.execute(select(Team.id).where(
                Team.account_id == space_id,
                func.lower(func.trim(Team.email)) == email.lower(),
            ))).first()
            if is_owner:
                continue
            account = (await db.execute(select(AccountPoolEntry).where(
                func.lower(func.trim(AccountPoolEntry.email)) == email.lower(),
                AccountPoolEntry.deleted_at.is_(None),
            ))).scalar_one_or_none()
            if account is None or not account.password_encrypted or not account.two_factor_secret_encrypted:
                continue
            version = (account.password_encrypted, account.two_factor_secret_encrypted)
            try:
                login = await authorization_service.login_entry(db, account.id, space_id)
                saved = await authorization_service.save_result(
                    db, account.id, login, credential_version=version,
                    liveness=("alive", "密码、2FA 与 OAuth 登录验证通过"),
                    update_current=account.workspace_id == space_id,
                )
                if not saved:
                    continue
                usages.update(await usage_service.check_many(db, [key]))
            except Exception as exc:
                usages[key] = {"status": "invalid", "error": f"授权刷新失败：{exc}"}
    refreshed = []
    for entry in entries:
        if entry.get("status") != "joined":
            status = "invited" if entry.get("status") == "invited" else (
                "conflict" if entry.get("status") == "conflict" else "not_joined"
            )
            refreshed.append({**entry, "usage": {"status": status}})
            continue
        email, space_id = _quota_key(entry)
        snapshot = await store_quota(
            db, email=email, space_id=space_id, seat_type=entry.get("seat_type"),
            team_id=entry.get("quota_team_id", entry.get("workspace_team_id")),
            usage=usages[(email, space_id)],
        )
        refreshed.append({**entry, "usage": snapshot_usage(snapshot)})
    await db.commit()
    details = await _usage_details(db, entries)
    return [{**entry, "team_usage": details.get(_quota_key(entry))} for entry in refreshed]
