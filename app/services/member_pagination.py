"""Read bounded batches without treating a partial member list as complete."""

MEMBER_PAGE_SIZE = 50
MEMBER_BATCH_PAGES = 10


def pagination_error(message):
    return {"success": False, "members": [], "total": 0, "error": message}


def validate_page(data, offset):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("成员列表响应格式无效")
    items, total = data["items"], data.get("total")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("成员资料格式无效")
    if type(total) is not int or total < 0:
        raise ValueError("成员列表总数无效")
    if not items and offset < total:
        raise ValueError("成员列表分页不完整：上游返回空页")
    return items, total


async def fetch_member_batch(fetch_page, *, offset=0):
    members = []
    next_offset = offset
    for page_index in range(MEMBER_BATCH_PAGES):
        result = await fetch_page(next_offset)
        if not result["success"]:
            return {**result, "members": [], "total": 0}
        try:
            items, total = validate_page(result["data"], next_offset)
        except ValueError as exc:
            return pagination_error(str(exc))
        members.extend(items)
        next_offset += len(items)
        if next_offset >= total:
            break
    return {
        "success": True, "members": members, "total": total, "error": None,
        "pagination": {"next_offset": next_offset, "has_more": next_offset < total,
                       "pages_fetched": page_index + 1},
    }


async def fetch_all_members(fetch_batch):
    members, offset = [], 0
    while True:
        result = await fetch_batch(offset)
        if not result["success"]:
            return result
        members.extend(result["members"])
        pagination = result["pagination"]
        if not pagination["has_more"]:
            return {"success": True, "members": members, "total": len(members), "error": None}
        offset = pagination["next_offset"]
