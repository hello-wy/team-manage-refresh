"""Verify managed Team memberships and persist a fresh workspace discovery."""
import json

from sqlalchemy import func, or_, select, update

from app.models import AccountPoolEntry, AccountPoolWorkspace, Team, TeamEmailMapping
from app.utils.team_names import team_display_name
from app.utils.time_utils import get_now


class WorkspaceSyncError(ValueError):
    pass


async def synchronize_memberships(db, entry, *, discovered, team_service, authorize_identity):
    teams = await _candidate_teams(db, entry, discovered)
    options = {item["id"]: item for item in discovered}
    for team in teams:
        snapshot = await team_service.get_team_members(team.id, db)
        if not snapshot.get("success"):
            if _is_revoked_token_error(snapshot) and team.account_id in options:
                # The member's fresh OAuth workspace selection is authoritative here;
                # keep other owner-list failures visible instead of masking them.
                await authorize_identity(team.account_id)
                options[team.account_id] = {
                    "id": team.account_id, "name": team_display_name(team), "is_personal": False,
                }
                continue
            raise WorkspaceSyncError(
                f"{team_display_name(team)} 成员核对失败：{snapshot.get('error') or '未返回成员列表'}"
            )
        if _has_hidden_member(snapshot, entry.email):
            await authorize_identity(team.account_id)
            snapshot = await team_service.get_team_members(team.id, db)
            if not snapshot.get("success"):
                raise WorkspaceSyncError(f"{team_display_name(team)} 成员资料重新核对失败：{snapshot.get('error')}")
            if _has_hidden_member(snapshot, entry.email):
                raise WorkspaceSyncError(f"{team_display_name(team)} 授权用户 ID 未匹配实时成员，无法确认入组状态")
        joined = any(
            str(member.get("email") or "").strip().lower() == entry.email
            and member.get("status") == "joined"
            for member in snapshot["members"]
        )
        options.pop(team.account_id, None)
        if joined:
            options[team.account_id] = {
                "id": team.account_id, "name": team_display_name(team), "is_personal": False,
            }
    return list(options.values())


def _is_revoked_token_error(snapshot):
    error_code = str(snapshot.get("error_code") or "").strip().lower()
    error_text = str(snapshot.get("error") or "").lower()
    return error_code in {"token_revoked", "token_invalidated"} or any(
        marker in error_text for marker in ("token_revoked", "token_invalidated", "invalidated oauth token")
    )


def _has_hidden_member(snapshot, email):
    joined = [member for member in snapshot["members"] if member.get("status") == "joined"]
    return not any(member.get("email") == email for member in joined) and any(
        not member.get("email") for member in joined
    )


async def _candidate_teams(db, entry, discovered):
    linked = select(TeamEmailMapping.team_id).where(
        TeamEmailMapping.email == entry.email, TeamEmailMapping.status.in_(("joined", "invited")),
    )
    ids = [item["id"] for item in discovered if not item["is_personal"]]
    teams = (await db.execute(select(Team).where(or_(
        Team.account_id.in_(ids), Team.id.in_(linked),
        func.lower(func.trim(Team.email)) == entry.email,
    )).order_by(Team.id))).scalars().all()
    unique = {}
    for team in teams:
        unique.setdefault(team.account_id, team)
    return list(unique.values())


def _selected_workspace(entry, options):
    ids = {item["id"] for item in options}
    selected = entry.workspace_id if entry.workspace_id in ids else None
    return next((item for item in options if item["id"] == selected),
                options[0] if options else None)


def _export_state(entry, saved, selected):
    if saved:
        return {"export_json_encrypted": saved.export_json_encrypted,
                "export_json_updated_at": saved.json_updated_at}
    if selected == entry.workspace_id:
        return {"export_json_encrypted": entry.export_json_encrypted,
                "export_json_updated_at": entry.export_json_updated_at}
    return {"export_json_encrypted": None, "export_json_updated_at": None}


async def persist_workspace_scan(db, entry, *, discovered, credential_version):
    teams = [item for item in discovered if not item["is_personal"]]
    options = teams or discovered
    current = _selected_workspace(entry, options)
    selected = current["id"] if current else None
    saved = (await db.execute(select(AccountPoolWorkspace).where(
        AccountPoolWorkspace.account_pool_id == entry.id,
        AccountPoolWorkspace.workspace_id == selected,
    ))).scalar_one_or_none() if selected else None
    state = {"available_workspaces": options}
    updated = await db.execute(update(AccountPoolEntry).where(
        AccountPoolEntry.id == entry.id, AccountPoolEntry.deleted_at.is_(None),
        AccountPoolEntry.password_encrypted == credential_version[0],
        AccountPoolEntry.two_factor_secret_encrypted == credential_version[1],
    ).values(
        workspace_id=selected,
        workspace_name=current["name"] if current else None,
        workspace_status=("personal_account" if current["is_personal"] else "workspace_ok")
        if current else "no_workspace",
        workspace_checked_at=get_now(),
        workspace_state_json=json.dumps(state, ensure_ascii=False),
        **_export_state(entry, saved, selected),
    ))
    if not updated.rowcount:
        raise WorkspaceSyncError("账号凭据已变化，请重新刷新 Team")
    await db.commit()
    return state
