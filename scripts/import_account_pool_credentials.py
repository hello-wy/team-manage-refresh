"""从 stdin 读取 sub2api 导出 JSON，并导入账号号池凭据。"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import AsyncSessionLocal, close_db
from app.services.account_pool_credentials import account_pool_credential_service


def extract_names(payload: Any) -> list[str]:
    if isinstance(payload, list):
        if all(isinstance(item, str) for item in payload):
            return payload
        accounts = payload
    elif isinstance(payload, dict):
        accounts = payload.get("accounts")
    else:
        accounts = None
    if not isinstance(accounts, list):
        raise ValueError("输入必须是 sub2api 导出对象、账号数组或 name 字符串数组")
    names = [item.get("name") for item in accounts if isinstance(item, dict)]
    if len(names) != len(accounts) or not all(isinstance(name, str) for name in names):
        raise ValueError("每个账号都必须包含字符串类型的 name")
    return names


async def import_credentials(names: list[str]) -> dict[str, object]:
    try:
        async with AsyncSessionLocal() as db_session:
            return await account_pool_credential_service.import_export_names(
                db_session,
                names=names,
            )
    finally:
        await close_db()


def main() -> None:
    payload = json.load(sys.stdin)
    result = asyncio.run(import_credentials(extract_names(payload)))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
