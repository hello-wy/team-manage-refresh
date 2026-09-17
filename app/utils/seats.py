"""成员席位类型及实时数量统计。"""


def normalize_seat_type(value):
    if not isinstance(value, str):
        return "unknown"
    value = value.strip().lower()
    if value in {"default", "standard"}:
        return "standard"
    return "premium" if value in {"premium", "prolite"} else "unknown"


def upstream_seat_type(value):
    return {"standard": "default", "premium": "prolite"}.get(normalize_seat_type(value))


def total_paid_seats(capacity):
    """合计标准和高级已购席位；配额不完整时返回未知，不使用本地上限。"""
    if not isinstance(capacity, list):
        return None
    paid_by_kind = {}
    for row in capacity:
        if not isinstance(row, dict):
            return None
        kind = normalize_seat_type(row.get("type"))
        if kind == "unknown":
            continue
        paid = row.get("paid")
        if kind in paid_by_kind or type(paid) is not int or paid < 0:
            return None
        paid_by_kind[kind] = paid
    if set(paid_by_kind) != {"standard", "premium"}:
        return None
    return sum(paid_by_kind.values())


def calculate_seat_balance(capacity, members, invites, holds=()):
    """余额取官方可用与成员占用核算的较小值，再扣除邀请及本地预留。"""
    rows = {}
    for row in capacity if isinstance(capacity, list) else []:
        kind = normalize_seat_type(row.get("type")) if isinstance(row, dict) else "unknown"
        if kind != "unknown":
            if kind in rows:
                return {"success": False, "error": "席位配额数据重复，暂时无法确认余额"}
            rows[kind] = row
    joined = {kind: 0 for kind in ("standard", "premium")}
    pending = dict(joined)
    invited = dict(joined)
    reserved = dict(joined)
    for member in members:
        kind = normalize_seat_type(member.get("seat_type"))
        if kind in joined:
            joined[kind] += 1
        elif member.get("seat_type") != "usage_based":
            return {"success": False, "error": "部分成员席位类型未知，暂时无法确认余额"}
        pending_kind = normalize_seat_type(member.get("pending_seat_type"))
        if pending_kind in pending and pending_kind != kind:
            pending[pending_kind] += 1
        elif pending_kind == "unknown" and member.get("pending_seat_type") not in (None, "", "usage_based"):
            return {"success": False, "error": "部分待生效席位类型未知，暂时无法确认余额"}
    for invite in invites:
        kind = normalize_seat_type(invite.get("seat_type"))
        if kind in invited:
            invited[kind] += 1
        elif invite.get("seat_type") != "usage_based":
            return {"success": False, "error": "部分邀请席位类型未知，暂时无法确认余额"}
    for hold in holds:
        kind = normalize_seat_type(hold.seat_type)
        if kind in reserved:
            reserved[kind] += 1
    balance = {}
    for kind in joined:
        row = rows.get(kind, {})
        paid, available = row.get("paid"), row.get("available")
        known = type(paid) is int and type(available) is int and 0 <= available <= paid
        usable = min(available, max(0, paid - joined[kind] - pending[kind])) if known else None
        balance[kind] = {
            "known": known, "paid": paid if known else None,
            "available": available if known else None,
            "joined": joined[kind], "pending": pending[kind],
            "invited": invited[kind], "reserved": reserved[kind],
            "remaining": max(0, usable - invited[kind] - reserved[kind]) if known else None,
        }
    return {"success": True, "balance": balance, "error": None}


def summarize_member_seats(members, invites_complete=True):
    counts = {
        status: {"standard": 0, "premium": 0, "unknown": 0, "total": 0}
        for status in ("joined", "invited")
    }
    for member in members:
        bucket = counts.get(member.get("status"))
        if bucket is None:
            continue
        bucket[normalize_seat_type(member.get("seat_type"))] += 1
        bucket["total"] += 1
    return {**counts, "invites_complete": invites_complete}
