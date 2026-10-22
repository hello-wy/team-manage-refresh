"""Shared timed-departure policy, including standard-seat opt-in."""
from datetime import timedelta

from sqlalchemy import or_

from app.models import TeamEmailMapping
from app.utils.seats import normalize_seat_type

OWNER_ROLE = "account-owner"


def is_auto_kick_exempt(mapping):
    if mapping.auto_kick_exempt or mapping.member_role == OWNER_ROLE:
        return True
    return (normalize_seat_type(mapping.seat_type) == "standard"
            and not mapping.standard_auto_kick_enabled)


def auto_kick_enabled_condition():
    return (TeamEmailMapping.auto_kick_exempt.is_(False) & or_(
        TeamEmailMapping.seat_type.is_(None),
        TeamEmailMapping.seat_type.not_in(("standard", "default")),
        TeamEmailMapping.standard_auto_kick_enabled.is_(True),
    ))


def calculate_deadline(mapping, hours):
    if is_auto_kick_exempt(mapping) or not mapping.joined_at:
        return None
    return mapping.joined_at + timedelta(hours=hours)
