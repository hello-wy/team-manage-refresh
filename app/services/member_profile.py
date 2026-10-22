"""Normalize member profiles before reconciling membership and exit schedules."""
from datetime import datetime
from math import isfinite

import pytz

MILLISECONDS_PER_SECOND = 1000
MILLISECOND_TIMESTAMP_THRESHOLD = 100_000_000_000


def parse_member_joined_at(value, timezone):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("加入时间不能是布尔值")
    text = str(value).strip()
    numeric = text.replace(".", "", 1).lstrip("-").isdigit()
    if numeric:
        timestamp = float(text)
        if not isfinite(timestamp):
            raise ValueError("加入时间不是有效时间戳")
        if abs(timestamp) >= MILLISECOND_TIMESTAMP_THRESHOLD:
            timestamp /= MILLISECONDS_PER_SECOND
        return datetime.fromtimestamp(timestamp, timezone).replace(tzinfo=None)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone).replace(tzinfo=None)
    return parsed


def normalize_member_profiles(members, mappings, timezone_name, *, identities=None):
    timezone = pytz.timezone(timezone_name)
    by_user_id = {
        mapping.upstream_user_id: mapping for mapping in mappings.values()
        if mapping.status == "joined" and mapping.upstream_user_id
    }
    profiles = [{**member, "email": member.get("email") or (identities or {}).get(member.get("id"))}
                for member in members]
    return [_normalize_profile(member, mappings, by_user_id, timezone=timezone)
            for member in profiles]


def _current_mapping(member, mapping):
    if not mapping or mapping.status != "joined":
        return None
    same_user = (
        not member.get("id") or not mapping.upstream_user_id
        or member["id"] == mapping.upstream_user_id
    )
    return mapping if same_user else None


def _joined_at(member, cached, timezone):
    errors = []
    joined_at = cached.joined_at if cached else None
    try:
        joined_at = parse_member_joined_at(member.get("created_time"), timezone) or joined_at
    except (ValueError, OverflowError, OSError):
        errors.append("上游加入时间格式无效")
    if joined_at is None:
        errors.append("缺少加入时间，无法计算下线时间")
    return joined_at, errors


def _cached_fields(member, cached):
    if not cached:
        return []
    fields = (
        ("email", cached.email, "邮箱"),
        ("name", cached.member_name, "姓名"),
        ("created_time", cached.joined_at, "加入时间"),
        ("role", cached.member_role, "角色"),
        ("seat_type", cached.seat_type, "席位类型"),
    )
    return [label for key, saved, label in fields
            if saved and (not member.get(key) or (key == "email" and member.get("profile_cached_email")))]


def _normalize_profile(member, mappings, by_user_id, *, timezone):
    email = str(member.get("email") or "").strip().lower()
    mapping = mappings.get(email) if email else by_user_id.get(member.get("id"))
    cached = _current_mapping(member, mapping)
    email = email or (cached.email if cached else None)
    joined_at, errors = _joined_at(member, cached, timezone)
    if member.get("identity_resolution_error"):
        errors.append(member["identity_resolution_error"])
    if not email:
        errors.append("缺少成员邮箱，无法同步下线计划")
    return {
        **member,
        "email": email or None,
        "name": member.get("name") or (cached.member_name if cached else None),
        "role": member.get("role") or (cached.member_role if cached else None),
        "seat_type": member.get("seat_type") or (cached.seat_type if cached else None),
        "created_time": joined_at.isoformat() if joined_at else None,
        "profile_sync_error": "；".join(errors) or None,
        "profile_cached_fields": _cached_fields(member, cached),
    }
