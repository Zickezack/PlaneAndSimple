"""Read side of the global query log (entries are written by `ingestion.run_query`)."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flighttracker.models import QueryLog, QueryOutcome, Search

PAGE_SIZE = 100


@dataclass(frozen=True)
class LogPage:
    entries: list[QueryLog]
    # id to pass as `before` for the next (older) page, None on the last page.
    next_before: int | None
    # Suchabos of the shown entries that still exist (for links).
    existing_search_ids: set[int]


def list_entries(
    session: Session,
    *,
    search_id: int | None = None,
    outcome: QueryOutcome | None = None,
    before: int | None = None,
    limit: int = PAGE_SIZE,
) -> LogPage:
    """Newest first; paged by id so that new entries never shift the pages."""
    statement = select(QueryLog).order_by(QueryLog.id.desc()).limit(limit + 1)
    if search_id is not None:
        statement = statement.where(QueryLog.search_id == search_id)
    if outcome is not None:
        statement = statement.where(QueryLog.outcome == outcome)
    if before is not None:
        statement = statement.where(QueryLog.id < before)
    rows = list(session.scalars(statement))
    entries = rows[:limit]
    search_ids = {e.search_id for e in entries if e.search_id is not None}
    existing = (
        set(session.scalars(select(Search.id).where(Search.id.in_(search_ids))))
        if search_ids
        else set()
    )
    next_before = entries[-1].id if len(rows) > limit else None
    return LogPage(entries, next_before, existing)


def logged_searches(session: Session) -> list[tuple[int, str]]:
    """(id, latest name) of every Suchabo that appears in the log, for the filter."""
    latest = func.max(QueryLog.id).label("latest")
    newest = select(QueryLog.search_id, latest).group_by(QueryLog.search_id).subquery()
    rows = session.execute(
        select(QueryLog.search_id, QueryLog.search_name)
        .join(newest, QueryLog.id == newest.c.latest)
        .where(QueryLog.search_id.is_not(None))
        .order_by(QueryLog.search_name)
    )
    return [(search_id, name) for search_id, name in rows]
