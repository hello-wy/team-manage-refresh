"""Build pool profile updates from the authenticated user's OAuth claims."""
import json
import unicodedata

from sqlalchemy import update

from app.models import AccountPoolEntry

from app.services.openai_workspace import token_claims, token_plan_type
from app.services.openai_subscription import personal_subscription

MAX_ACCOUNT_NAME_LENGTH = 255
PROFILE_CLAIM = "https://api.openai.com/profile"


def account_name_key(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("账号名称必须是文本")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    if len(value) > MAX_ACCOUNT_NAME_LENGTH or len(normalized) > MAX_ACCOUNT_NAME_LENGTH:
        raise ValueError("账号名称超出存储长度")
    return normalized or None


def credential_account_name(payload, expected_email):
    credentials = payload["accounts"][0]["credentials"] if "accounts" in payload else payload
    for field in ("id_token", "access_token"):
        claims = token_claims(credentials.get(field, ""))
        profile = claims.get(PROFILE_CLAIM) or {}
        if not isinstance(profile, dict):
            raise ValueError("账号资料声明格式无效")
        email = str(claims.get("email") or profile.get("email") or "").strip().lower()
        if email and email != expected_email.strip().lower():
            raise ValueError("账号名称凭据的邮箱与号池账号不一致")
        name = claims.get("name") or profile.get("name")
        if name and email:
            if account_name_key(name):
                return name.strip()
    return None


def login_result_values(entry, result, *, cipher, now, update_current, workspace_state):
    values = {"workspace_checked_at": now}
    if update_current:
        previous = json.loads(entry.workspace_state_json) if entry.workspace_state_json else {}
        current_state = dict(result.workspace)
        if not current_state.get("personal_plan"):
            current_state.pop("personal_plan", None)
        full_state = {**previous, **(workspace_state or {}), **current_state}
        options = (workspace_state if workspace_state is not None else previous).get("available_workspaces")
        if options is not None:
            full_state["available_workspaces"] = [
                {**item, "personal_plan": result.workspace["personal_plan"]}
                if item.get("id") == result.workspace.get("workspace_id")
                and result.workspace.get("personal_plan") else item
                for item in options
            ]
        values = {
            **values, "workspace_id": result.workspace.get("workspace_id") or None,
            "workspace_name": result.workspace.get("workspace_name") or None,
            "workspace_status": result.workspace.get("status") or "workspace_unknown",
            "workspace_state_json": json.dumps(full_state, ensure_ascii=False),
            "export_json_encrypted": cipher.encrypt_token(json.dumps(result.payload, ensure_ascii=False)),
            "export_json_updated_at": now,
        }
    return values


def credential_personal_plan(payload):
    credentials = payload["accounts"][0]["credentials"] if "accounts" in payload else payload
    access = token_claims(credentials.get("access_token", ""))
    primary_plan = token_plan_type(access, credentials)
    if primary_plan and primary_plan != "personal" and not personal_subscription({"plan_type": primary_plan}):
        return ""
    return (personal_subscription(credentials) or personal_subscription(access)
            or personal_subscription(token_claims(credentials.get("id_token", ""))))


async def save_import_account_name(db, entry_id, payload):
    entry = await db.get(AccountPoolEntry, entry_id)
    if entry is None or entry.deleted_at is not None:
        raise ValueError("账号不存在或已删除，无法保存导入名称")
    if entry.name is not None:
        return False
    name = credential_account_name(payload, entry.email)
    if name is None:
        raise ValueError("导入登录未返回账号姓名")
    values = {"name": name, "name_key": account_name_key(name)}
    plan = credential_personal_plan(payload)
    if plan:
        state = json.loads(entry.workspace_state_json) if entry.workspace_state_json else {}
        values["workspace_state_json"] = json.dumps({**state, "personal_plan": plan}, ensure_ascii=False)
    result = await db.execute(update(AccountPoolEntry).where(
        AccountPoolEntry.id == entry_id, AccountPoolEntry.deleted_at.is_(None),
        AccountPoolEntry.name.is_(None),
    ).values(**values))
    return bool(result.rowcount)
