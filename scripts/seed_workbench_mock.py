"""为项目内 SQLite 添加工作台调试数据；重复运行跳过已有记录，--remove 清理。"""
import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import AsyncSessionLocal, close_db
from app.models import Team
from app.utils.time_utils import get_now


PREFIX = "mock-workbench-v1-"
# 标签、本地人数、已加入人数、已购席位、原始状态
SCENARIOS = [
    ("available", "可用", 1, 1, 3, "active"),
    ("upstream-full", "上游席位已满 2/2", 2, 2, 2, "active"),
    ("overbooked", "超额成员 3/2", 3, 3, 2, "active"),
    ("local-full", "本地上限已满", 6, 3, 10, "active"),
    ("zero-seats", "无可用席位 0/0", 0, 0, 0, "active"),
    ("unknown", "席位数据未知", 1, 1, None, "active"),
    ("banned", "已封禁且满员", 2, 2, 2, "banned"),
    ("expired", "已过期且满员", 2, 2, 2, "expired"),
    ("error", "异常且满员", 2, 2, 2, "error"),
]


async def seed(remove=False):
    try:
        async with AsyncSessionLocal() as session:
            changed = 0
            for pool in ("normal", "welfare"):
                scenarios = SCENARIOS if pool == "normal" else SCENARIOS[:2]
                for key, label, local, joined, paid, status in scenarios:
                    account_id = f"{PREFIX}{pool}-{key}"
                    email = f"{account_id}@example.invalid"
                    existing = (await session.execute(select(Team).where(
                        Team.account_id == account_id, Team.email == email,
                    ))).scalars().all()
                    if remove:
                        for team in existing:
                            await session.delete(team)
                            changed += 1
                    elif not existing:
                        session.add(Team(
                            email=email, account_id=account_id, team_name=f"[MOCK] {label}",
                            # 空凭据不会提供任何真实账号访问能力。
                            access_token_encrypted="", plan_type="team", subscription_plan="team",
                            current_members=local, max_members=6, joined_members=joined,
                            total_seats=paid, status=status, pool_type=pool,
                            expires_at=get_now() + timedelta(days=-1 if status == "expired" else 30),
                            last_sync=get_now(), rotation_mode="off", member_auto_kick_hours=0,
                        ))
                        changed += 1
            await session.commit()
            print(f"{'清理' if remove else '新增'} {changed} 条 mock Team；已有记录不会重复添加。")
    finally:
        await close_db()


def main(seed_callback=seed, description=__doc__):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--remove", action="store_true", help="只删除本脚本创建的 mock 数据")
    args = parser.parse_args()
    url = make_url(settings.database_url)
    root = Path(__file__).resolve().parent.parent
    path = Path(url.database or "").resolve()
    if url.get_backend_name() != "sqlite" or not path.is_relative_to(root) or not path.is_file():
        parser.error("仅支持项目目录内已初始化的本地 SQLite 数据库")
    backup = path.with_name(f"{path.name}.mock-backup-{datetime.now():%Y%m%d-%H%M%S-%f}.bak")
    with sqlite3.connect(str(path)) as source, sqlite3.connect(str(backup)) as target:
        source.backup(target)
    print(f"数据库: {path}\n备份: {backup}")
    asyncio.run(seed_callback(args.remove))


if __name__ == "__main__":
    main()
