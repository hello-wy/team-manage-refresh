"""Carry authenticated upstream snapshots between stateless batch requests."""
import json

from cryptography.fernet import InvalidToken


def read_sync_cursor(cursor, team, *, decrypt):
    if cursor is None:
        return {"offset": 0, "members": []}
    try:
        state = json.loads(decrypt(cursor))
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ValueError("成员同步游标无效，请重新打开成员管理") from exc
    if not isinstance(state, dict):
        raise ValueError("成员同步游标格式无效")
    if state.get("team_id") != team.id or state.get("account_id") != team.account_id:
        raise ValueError("成员同步游标与当前 Team 不匹配")
    if type(state.get("offset")) is not int or state["offset"] < 0:
        raise ValueError("成员同步位置无效")
    if not isinstance(state.get("members"), list):
        raise ValueError("成员同步快照无效")
    return state


async def fetch_sync_members(remote, context, *, cursor, cipher):
    team, token, db = context
    state = read_sync_cursor(cursor, team, decrypt=cipher.decrypt_token)
    result = await remote.get_members_batch(token, team.account_id, db, offset=state["offset"])
    if not result["success"]:
        return result
    members = [*state["members"], *result["members"]]
    pagination = result["pagination"]
    next_cursor = None
    if pagination["has_more"]:
        next_cursor = cipher.encrypt_token(json.dumps({
            "team_id": team.id, "account_id": team.account_id,
            "offset": pagination["next_offset"], "members": members,
        }, ensure_ascii=False))
    return {**result, "members": members, "pagination": {
        **pagination, "next_cursor": next_cursor, "loaded": len(members),
        "total": result["total"],
    }}
