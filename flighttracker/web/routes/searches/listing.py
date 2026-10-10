"""List of tracked searches (with Trips and their legs)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from flighttracker.domain.locations import describe_locations
from flighttracker.models import LocationRole, Search
from flighttracker.services import history
from flighttracker.services.access import (
    visible_searches,
    visible_trips,
)
from flighttracker.services.searches import (
    list_searches,
    spec_of,
)
from flighttracker.services.trips import (
    list_trips,
    trip_search_ids,
)
from flighttracker.services.users import usernames
from flighttracker.web.deps import (
    AuthUser,
    get_db,
    require_login,
)
from flighttracker.web.routes.searches._common import (
    redirect,
)
from flighttracker.web.templating import templates

router = APIRouter()


def _location_text(search: Search, role: LocationRole) -> str:
    spec = spec_of(search)
    refs = spec.origins if role == LocationRole.ORIGIN else spec.destinations
    return describe_locations(refs, spec.country_airports)


@router.get("/")
def index() -> Response:
    return redirect("/searches")


@router.get("/searches")
def search_list(
    request: Request,
    archived: bool = False,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    all_searches = list_searches(db, archived=archived, visible=visible_searches(user))
    trips = list_trips(db, archived=archived, visible=visible_trips(user))
    trip_ids = trip_search_ids(db, trips)
    counts = history.observation_counts(db, [s.id for s in all_searches])
    searches = [search for search in all_searches if search.id not in trip_ids]
    owners = {item.owner_id for item in [*searches, *trips] if item.owner_id is not None}
    return templates.TemplateResponse(
        request,
        "searches/list.html",
        {
            "searches": searches,
            "trips": trips,
            "counts": counts,
            "archived": archived,
            "location_text": _location_text,
            "owner_names": usernames(db, owners),
        },
    )
