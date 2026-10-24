"""Timed departure: joined time plus the Team's hours, for members that participate."""
from datetime import timedelta

from sqlalchemy import func

from app.models import TeamEmailMapping

OWNER_ROLE = "account-owner"
DEFAULT_AUTO_KICK_HOURS = 2
MIN_AUTO_KICK_HOURS = 1
MAX_AUTO_KICK_HOURS = 8760
RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
MAX_ERROR_LENGTH = 500


def team_auto_kick_hours(team):
    return int((team.member_auto_kick_hours if team else None) or DEFAULT_AUTO_KICK_HOURS)


def participates(mapping):
    return mapping.member_role != OWNER_ROLE and (
        getattr(mapping, "departure_policy", None) == "team_expiry" or mapping.auto_kick_enabled is not False)


def has_auto_kick_schedule(mapping):
    return bool(mapping.auto_kick_at) and participates(mapping)


def auto_kick_enabled_condition():
    return (TeamEmailMapping.auto_kick_at.is_not(None)
            & TeamEmailMapping.auto_kick_enabled.is_(True)
            & (func.coalesce(TeamEmailMapping.member_role, "") != OWNER_ROLE))


def _reset_failures(mapping):
    mapping.auto_kick_failures = 0
    mapping.auto_kick_last_error = None
    mapping.auto_kick_retry_at = None


def refresh_deadline(mapping, hours):
    if getattr(mapping, "departure_policy", None) == "team_expiry":
        return
    start = getattr(mapping, "usage_started_at", None) or mapping.joined_at
    deadline = start + timedelta(hours=hours) if participates(mapping) and start else None
    if deadline != mapping.auto_kick_at:
        mapping.auto_kick_at = deadline
        _reset_failures(mapping)


def set_participation(mapping, enabled, hours):
    mapping.auto_kick_enabled = bool(enabled)
    # 额度轮转把豁免成员视为受保护成员，与是否参与定时下线保持一致。
    mapping.auto_kick_exempt = not enabled
    refresh_deadline(mapping, hours)


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
