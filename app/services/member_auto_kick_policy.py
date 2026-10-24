"""高级席位按使用小时下线，标准席位按 Team 到期下线。"""
from datetime import timedelta

from sqlalchemy import case, func, or_

from app.models import Team, TeamEmailMapping

OWNER_ROLE = "account-owner"
EXPIRY_POLICIES = ("team_expiry", "standard_expiry")
DEFAULT_AUTO_KICK_HOURS = 2
MIN_AUTO_KICK_HOURS = 1
MAX_AUTO_KICK_HOURS = 8760
RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
MAX_ERROR_LENGTH = 500


def team_auto_kick_hours(team):
    return int((team.member_auto_kick_hours if team else None) or DEFAULT_AUTO_KICK_HOURS)


def participates(mapping):
    return mapping.member_role != OWNER_ROLE and (
        getattr(mapping, "departure_policy", None) in EXPIRY_POLICIES
        or mapping.seat_type == "standard"
        or (mapping.seat_type == "premium" and mapping.auto_kick_enabled is not False))


def departure_deadline(mapping, team=None):
    if mapping.member_role == OWNER_ROLE or mapping.status != "joined":
        return None
    if mapping.seat_type == "standard" or getattr(mapping, "departure_policy", None) in EXPIRY_POLICIES:
        return team.expires_at if team is not None else mapping.auto_kick_at
    return mapping.auto_kick_at if participates(mapping) else None


def has_auto_kick_schedule(mapping, team=None):
    return bool(departure_deadline(mapping, team))


def auto_kick_deadline_expression():
    return case((or_(TeamEmailMapping.seat_type == "standard",
                     TeamEmailMapping.departure_policy == "team_expiry"), Team.expires_at),
                else_=TeamEmailMapping.auto_kick_at)


def auto_kick_enabled_condition():
    return (auto_kick_deadline_expression().is_not(None)
            & or_(TeamEmailMapping.seat_type == "standard",
                  TeamEmailMapping.departure_policy == "team_expiry",
                  (TeamEmailMapping.seat_type == "premium") & TeamEmailMapping.auto_kick_enabled.is_(True))
            & (func.coalesce(TeamEmailMapping.member_role, "") != OWNER_ROLE))


def _reset_failures(mapping):
    mapping.auto_kick_failures = 0
    mapping.auto_kick_last_error = None
    mapping.auto_kick_retry_at = None


def refresh_deadline(mapping, hours, *, team=None):
    if getattr(mapping, "departure_policy", None) == "team_expiry":
        return  # 质保规则由兑换记录保护流程设置。
    previous_policy = getattr(mapping, "departure_policy", None)
    mapping.departure_policy = "standard_expiry" if mapping.seat_type == "standard" else "timed"
    start = getattr(mapping, "usage_started_at", None) or mapping.joined_at
    if mapping.seat_type == "standard":
        deadline = team.expires_at if team and mapping.status == "joined" and mapping.member_role != OWNER_ROLE else None
    else:
        deadline = start + timedelta(hours=hours) if participates(mapping) and start else None
    if deadline != mapping.auto_kick_at or mapping.departure_policy != previous_policy:
        mapping.auto_kick_at = deadline
        _reset_failures(mapping)


def set_participation(mapping, enabled, hours, *, team=None):
    mapping.auto_kick_enabled = bool(enabled)
    mapping.auto_kick_exempt = not enabled
    refresh_deadline(mapping, hours, team=team)


def reset_for_new_join(mapping):
    mapping.departure_policy = "timed"
    mapping.usage_started_at = None
    mapping.auto_kick_enabled = True
    mapping.auto_kick_exempt = False
    mapping.auto_kick_at = None
    _reset_failures(mapping)


def record_failure(mapping, error, now):
    failures = int(mapping.auto_kick_failures or 0) + 1
    mapping.auto_kick_failures = failures
    mapping.auto_kick_last_error = (error or "自动下线失败")[:MAX_ERROR_LENGTH]
    mapping.auto_kick_retry_at = now + RETRY_DELAYS[min(failures, len(RETRY_DELAYS)) - 1]
