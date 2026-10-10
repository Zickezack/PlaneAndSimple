"""Helpers shared by the routes of tracked searches (loading with access check, quota, estimate)."""

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.spec import (
    SearchSpec,
    queries_per_poll,
    route_pairs,
)
from flighttracker.i18n import Msg
from flighttracker.models import Search, User
from flighttracker.services import quota
from flighttracker.services.access import (
    Access,
    search_access,
)
from flighttracker.services.searches import (
    get_search,
    spec_of,
)
from flighttracker.web.deps import (
    AuthUser,
)


def load_search(
    db: Session, search_id: int, user: AuthUser, needed: Access = Access.VIEW
) -> Search:
    """The Suchabo if `user` has at least `needed` access; 404 if they may not even see it."""
    search = get_search(db, search_id)
    access = search_access(db, user, search) if search is not None else None
    if access is None:
        raise HTTPException(status_code=404, detail="Tracked search not found.")
    if access < needed:
        raise HTTPException(status_code=403, detail="You may not change this tracked search.")
    return search


def check_quota(
    db: Session,
    settings: Settings,
    owner_id: int | None,
    *,
    added_searches: int = 0,
    added_requests: int = 0,
) -> list[Msg]:
    owner = db.get(User, owner_id) if owner_id is not None else None
    return quota.check(
        db, owner, settings, added_searches=added_searches, added_requests=added_requests
    )


def added_requests(search: Search, new_spec: SearchSpec, settings: Settings) -> int:
    """Change of the owner's requests per poll if `search` gets `new_spec` (archived: none)."""
    if search.is_archived:
        return 0
    old = quota.search_requests(spec_of(search), settings)
    return quota.search_requests(new_spec, settings) - old


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# Rough time per scraper request on top of the configured pause (measured: ~1 s).
_SECONDS_PER_REQUEST = 1.0


def estimate(spec: SearchSpec, settings: Settings) -> dict:
    """Cost of one poll, so that the user can weigh coverage against requests and time."""
    queries = queries_per_poll(spec)
    requests = quota.search_requests(spec, settings)
    seconds = requests * (settings.scraper_request_delay_seconds + _SECONDS_PER_REQUEST)
    return {
        "routes": len(route_pairs(spec)),
        "queries": queries,
        "requests": requests,
        "minutes": max(1, round(seconds / 60)),
        "stay_lengths": spec.filters.stay_lengths,
    }
