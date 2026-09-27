"""添加本地账号号池 mock 数据及关联记录；重复运行跳过已有账号，--remove 清理。"""
import json
from datetime import timedelta

from seed_workbench_mock import main
from sqlalchemy import delete, select

from app.database import AsyncSessionLocal, close_db
from app.models import (
    AccountPoolEntry, AccountPoolExportJob, AccountPoolHistory,
    AccountPoolTeamUsage, AccountPoolWorkspace, Team, TeamEmailMapping,
)
from app.services.encryption import encryption_service
from app.utils.time_utils import get_now


PREFIX = "mock-pool-v1-"
# 名称、验活状态、成员状态、席位类型
SCENARIOS = [
    ("premium", "alive", "joined", "premium"),
    ("standard", "alive", "joined", "standard"),
    ("invalid", "invalid", "joined", "standard"),
    ("invited", "alive", "invited", "standard"),
    ("conflict", "alive", "conflict", "premium"),
    ("login-error", "error", "unassigned", None),
    ("missing-password", "missing", "unassigned", None),
    ("unchecked", None, "unassigned", None),
    ("personal", "alive", "unassigned", None),
    ("left-team", "alive", "unassigned", None),
]


async def seed(remove=False):
    now = get_now()
    emails = [f"{PREFIX}{name}@example.invalid" for name, *_ in SCENARIOS]
    try:
        async with AsyncSessionLocal() as db:
            existing = {entry.email: entry for entry in (await db.execute(
                select(AccountPoolEntry).where(AccountPoolEntry.email.in_(emails))
            )).scalars()}
            team_ids = [f"{PREFIX}team-{key}" for key in ("a", "b")]
            teams = {team.account_id: team for team in (await db.execute(select(Team).where(
                Team.account_id.in_(team_ids),
                Team.email.in_([f"{key}@example.invalid" for key in team_ids]),
            ))).scalars()}
            if remove:
                await db.execute(delete(TeamEmailMapping).where(TeamEmailMapping.email.in_(emails)))
                for entry in existing.values():
                    await db.delete(entry)
                await db.flush()
                for team in teams.values():
                    await db.delete(team)
                await db.commit()
                print(f"已清理 {len(existing)} 条 mock 账号及其关联记录。")
                return

            for index, account_id in enumerate(team_ids):
                if account_id not in teams:
                    team = Team(
                        account_id=account_id, email=f"{account_id}@example.invalid",
                        team_name=f"[MOCK] 账号池调试 Team {'AB'[index]}",
                        access_token_encrypted="", status="active", pool_type="normal",
                        current_members=6 if index == 0 else 2, max_members=8,
                        joined_members=5 if index == 0 else 2, total_seats=8,
                        expires_at=now + timedelta(days=30), rotation_mode="off",
                        member_auto_kick_hours=0, last_sync=now,
                    )
                    db.add(team)
                    teams[account_id] = team
            await db.flush()
            primary = teams[team_ids[0]]
            count = 0
            for name, liveness, membership, seat_type in SCENARIOS:
                email = f"{PREFIX}{name}@example.invalid"
                if email in existing:
                    continue
                assigned = membership in ("joined", "invited", "conflict")
                selected_teams = list(teams.values()) if membership == "conflict" else ([primary] if assigned else [])
                spaces = [{"id": team.account_id, "name": team.team_name, "is_personal": False}
                          for team in selected_teams]
                if name == "personal":
                    spaces = [{"id": f"{PREFIX}personal", "name": "[MOCK] 个人空间", "is_personal": True}]
                workspace_status = "personal_account" if name == "personal" else (
                    "workspace_error" if name == "login-error" else "team" if assigned else "no_workspace"
                )
                entry = AccountPoolEntry(
                    email=email, liveness_status=liveness,
                    liveness_checked_at=now if liveness else None,
                    liveness_message=f"[MOCK] 调试数据：{name}，无真实登录凭据",
                    password_encrypted=encryption_service.encrypt_token("Mock-only-password-2026!")
                        if name not in ("missing-password", "unchecked") else None,
                    two_factor_secret_encrypted=encryption_service.encrypt_token("JBSWY3DPEHPK3PXP")
                        if name not in ("missing-password", "unchecked") else None,
                    workspace_id=spaces[0]["id"] if spaces else None,
                    workspace_name=spaces[0]["name"] if spaces else None,
                    workspace_status=workspace_status, workspace_checked_at=now,
                    workspace_state_json=json.dumps({"available_workspaces": spaces}),
                )
                db.add(entry)
                await db.flush()
                for space in spaces:
                    db.add(AccountPoolWorkspace(
                        account_pool_id=entry.id, workspace_id=space["id"], name=space["name"],
                        is_personal=space["is_personal"], status="discovered", checked_at=now,
                    ))
                for team in selected_teams:
                    mapping_status = "invited" if membership == "invited" else "joined"
                    db.add(TeamEmailMapping(
                        team_id=team.id, email=email, status=mapping_status, source="mock",
                        seat_type=seat_type, joined_at=now - timedelta(days=2) if mapping_status == "joined" else None,
                        auto_kick_exempt=True, is_admin_invited=True,
                    ))
                    if mapping_status == "joined":
                        db.add(AccountPoolHistory(
                            account_pool_id=entry.id, team_id=team.id, team_name=team.team_name,
                            team_email=team.email, joined_at=now - timedelta(days=2),
                        ))
                    db.add(AccountPoolTeamUsage(
                        account_pool_id=entry.id, team_id=team.id, team_space_id=team.account_id,
                        seat_type=seat_type, premium_used=seat_type == "premium", seat_switch_count=2,
                        seat_switched_at=now - timedelta(hours=6), quota_checked_at=now,
                        premium_used_at=now - timedelta(days=1) if seat_type == "premium" else None,
                        short_reset_at=(now + timedelta(hours=3)).isoformat(),
                        weekly_reset_at=(now + timedelta(days=4)).isoformat(),
                    ))
                if name == "left-team":
                    db.add(AccountPoolHistory(
                        account_pool_id=entry.id, team_id=primary.id, team_name=primary.team_name,
                        team_email=primary.email, joined_at=now - timedelta(days=10),
                        left_at=now - timedelta(days=8),
                    ))
                if name in ("premium", "invalid"):
                    db.add(AccountPoolExportJob(
                        account_pool_id=entry.id, workspace_id=primary.account_id,
                        status="completed" if name == "premium" else "failed", finished_at=now,
                        error="[MOCK] 登录凭据已失效，请更新凭据后重试" if name == "invalid" else None,
                    ))
                count += 1
            await db.commit()
            print(f"新增 {count} 条 mock 账号，关联 2 个独立 mock Team；已有账号跳过。")
    finally:
        await close_db()


if __name__ == "__main__":
    main(seed, __doc__)
