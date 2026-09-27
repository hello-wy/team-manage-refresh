"""Resolve human-readable names without showing workspace identifiers as names."""
from uuid import UUID


def readable_name(value, workspace_id=None):
    name = str(value or "").strip()
    if not name or name == workspace_id:
        return None
    try:
        UUID(name)
    except ValueError:
        return name
    return None


def team_display_name(team, fallback=None):
    return (readable_name(team.team_name, team.account_id)
            or readable_name(fallback, team.account_id)
            or f"Team #{team.id}")
