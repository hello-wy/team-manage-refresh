"""Resolve hidden emails only when the live name and pool identity are unique."""
from collections import Counter, defaultdict

from sqlalchemy import select

from app.models import AccountPoolEntry
from app.services.account_pool_profile import account_name_key


def _known_profiles(members, identities, *, mappings):
    by_user = {mapping.upstream_user_id: mapping.email for mapping in mappings.values()
               if mapping.status == "joined" and mapping.upstream_user_id}
    return [{**member, "email": member.get("email") or identities.get(member.get("id"))
             or by_user.get(member.get("id")),
             "profile_cached_email": bool(not member.get("email")
                                          and not identities.get(member.get("id"))
                                          and by_user.get(member.get("id")))} for member in members]


def _resolve_profile(member, candidates, *, names, known_emails):
    if member.get("email"):
        return member
    key = account_name_key(member.get("name"))
    matches = candidates.get(key, set())
    if not matches:
        return member
    if names[key] > 1:
        return {**member, "identity_resolution_error": "Team 内存在同名成员，无法通过名称唯一确认邮箱"}
    if len(matches) > 1:
        return {**member, "identity_resolution_error": "号池中存在同名账号，无法通过名称唯一确认邮箱"}
    email = next(iter(matches))
    if email in known_emails:
        return {**member, "identity_resolution_error": "名称匹配的邮箱已关联其他 Team 成员"}
    return {**member, "email": email}


async def resolve_pool_member_emails(db, members, *, identities, mappings):
    profiles = _known_profiles(members, identities, mappings=mappings)
    keys = {account_name_key(member.get("name")) for member in profiles if not member.get("email")}
    keys.discard(None)
    if not keys:
        return profiles
    rows = (await db.execute(select(AccountPoolEntry.name_key, AccountPoolEntry.email).where(
        AccountPoolEntry.name_key.in_(keys), AccountPoolEntry.deleted_at.is_(None),
    ))).all()
    candidates = defaultdict(set)
    for key, email in rows:
        candidates[key].add(email)
    names = Counter(account_name_key(member.get("name")) for member in profiles)
    known_emails = {str(member["email"]).strip().lower() for member in profiles if member.get("email")}
    return [_resolve_profile(member, candidates, names=names, known_emails=known_emails)
            for member in profiles]
