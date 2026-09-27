"""Filter and order pool identities in SQL before loading one page of account data."""
from sqlalchemy import case, func, literal, select, union_all

from app.models import AccountPoolEntry, Team, TeamEmailMapping
from app.utils.team_names import team_display_name

SORT_OPTIONS = {
    "team": "Team / 席位",
    "deadline": "下线时间优先",
    "newest": "最近添加",
    "oldest": "最早添加",
    "email": "邮箱 A–Z",
}


def _normalized(column):
    return func.lower(func.trim(column))


async def query_pool_ids(db, *, page, per_page, search, status_filter, team_filter, seat_filter, sort_by):
    sort_by = sort_by if sort_by in SORT_OPTIONS else "team"
    seat_filter = seat_filter if seat_filter in ("standard", "premium", "unknown") else ""
    status_filter = status_filter if status_filter in ("joined", "not_joined", "invited", "unassigned", "conflict") else ""
    team_filter = str(team_filter or "").strip()
    if team_filter != "personal":
        team_filter = str(int(team_filter)) if team_filter.isascii() and team_filter.isdecimal() and len(team_filter) <= 18 and int(team_filter) > 0 else ""

    pool = select(AccountPoolEntry.id, AccountPoolEntry.email, AccountPoolEntry.created_at,
                  AccountPoolEntry.updated_at).where(AccountPoolEntry.deleted_at.is_(None))
    owner_email = _normalized(Team.email)
    in_pool = select(AccountPoolEntry.id).where(
        AccountPoolEntry.deleted_at.is_(None), _normalized(AccountPoolEntry.email) == owner_email,
    ).exists()
    owners = select((-func.min(Team.id)).label("id"), owner_email.label("email"),
                    func.min(Team.created_at).label("created_at"),
                    func.min(Team.created_at).label("updated_at")).where(
        ~in_pool, owner_email != "",
    ).group_by(owner_email)
    identities = union_all(pool, owners).subquery()

    # Include owners without inventing a seat type; use a real joined mapping when available.
    joined = select(_normalized(TeamEmailMapping.email).label("email"), TeamEmailMapping.team_id,
                    TeamEmailMapping.seat_type,
                    case(((_normalized(TeamEmailMapping.email) != owner_email)
                           & (Team.rotation_mode == "off")
                           & TeamEmailMapping.auto_kick_exempt.is_(False)
                           & (func.coalesce(TeamEmailMapping.member_role, "") != "account-owner"),
                           TeamEmailMapping.auto_kick_at), else_=None).label("deadline"),
                    case((_normalized(TeamEmailMapping.email) == owner_email, 0),
                         (TeamEmailMapping.seat_type == "premium", 1),
                         (TeamEmailMapping.seat_type == "standard", 2), else_=3).label("seat_rank"),
                    literal("joined").label("status"),
    ).join(Team, Team.id == TeamEmailMapping.team_id).where(TeamEmailMapping.status == "joined")
    owner_mapping = select(TeamEmailMapping.id).where(
        TeamEmailMapping.team_id == Team.id, _normalized(TeamEmailMapping.email) == owner_email,
        TeamEmailMapping.status == "joined",
    ).exists()
    owner_memberships = select(owner_email.label("email"), Team.id.label("team_id"),
                               literal(None).label("seat_type"), literal(None).label("deadline"),
                               literal(0).label("seat_rank"), literal("joined").label("status"),
    ).where(~owner_mapping, owner_email != "")
    memberships = union_all(joined, owner_memberships).subquery()
    scope = select(memberships)
    if team_filter and team_filter != "personal":
        scope = scope.where(memberships.c.team_id == int(team_filter))
    scoped = scope.subquery()
    groups = select(scoped.c.email, func.min(scoped.c.team_id).label("team_id"),
                    func.min(scoped.c.deadline).label("deadline"),
    ).group_by(scoped.c.email).subquery()
    query = select(identities.c.id).outerjoin(groups, groups.c.email == identities.c.email)
    if search:
        query = query.where(identities.c.email.ilike(f"%{search.strip().lower()}%"))
    if team_filter == "personal":
        query = query.where(groups.c.team_id.is_(None))
    elif team_filter:
        query = query.where(groups.c.team_id.is_not(None))
    if seat_filter:
        matching_seat = select(scoped.c.email).where(scoped.c.email == identities.c.email,
                                                     scoped.c.seat_type == seat_filter).exists()
        if seat_filter == "unknown":
            matching_seat = ~select(scoped.c.email).where(
                scoped.c.email == identities.c.email, scoped.c.seat_type.in_(("standard", "premium")),
            ).exists()
        query = query.where(matching_seat)
    has_joined = select(memberships.c.email).where(memberships.c.email == identities.c.email).exists()
    has_invite = select(TeamEmailMapping.id).where(
        _normalized(TeamEmailMapping.email) == identities.c.email, TeamEmailMapping.status == "invited",
    ).exists()
    if status_filter == "joined":
        query = query.where(has_joined)
    elif status_filter == "not_joined":
        query = query.where(~has_joined)
    elif status_filter == "invited":
        query = query.where(has_invite, ~has_joined)
    elif status_filter == "unassigned":
        query = query.where(~has_joined, ~has_invite)
    elif status_filter == "conflict":
        active = union_all(select(memberships.c.email, memberships.c.team_id), select(
            _normalized(TeamEmailMapping.email).label("email"), TeamEmailMapping.team_id,
        ).where(TeamEmailMapping.status == "invited")).subquery()
        count = select(func.count(func.distinct(active.c.team_id))).where(
            active.c.email == identities.c.email).scalar_subquery()
        query = query.where(count > 1)

    primary_seat_rank = select(func.min(scoped.c.seat_rank)).where(
        scoped.c.email == identities.c.email, scoped.c.team_id == groups.c.team_id,
    ).correlate(identities, groups).scalar_subquery()
    orders = {
        "team": [groups.c.team_id.is_(None), groups.c.team_id, primary_seat_rank,
                 groups.c.deadline.is_(None), groups.c.deadline, identities.c.updated_at],
        "deadline": [groups.c.deadline.is_(None), groups.c.deadline, groups.c.team_id],
        "newest": [identities.c.created_at.desc(), identities.c.id.desc()],
        "oldest": [identities.c.created_at, identities.c.id],
        "email": [identities.c.email],
    }
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(max(page, 1), total_pages)
    ids = list((await db.execute(query.order_by(*orders[sort_by], identities.c.email, identities.c.id)
                                .offset((page - 1) * per_page).limit(per_page))).scalars())
    teams = (await db.execute(select(Team.id, Team.team_name, Team.account_id).order_by(Team.id))).all()
    return {"ids": ids, "total": total, "total_pages": total_pages, "current_page": page,
            "per_page": per_page, "sort_by": sort_by, "sort_options": SORT_OPTIONS,
            "team_filter": team_filter, "seat_filter": seat_filter, "status_filter": status_filter,
            "filter_teams": [{"id": team.id, "name": team_display_name(team)} for team in teams]}
