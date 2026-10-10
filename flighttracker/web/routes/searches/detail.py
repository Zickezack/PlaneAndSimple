"""Detail page (chart, heat map, flights), flight detail page and export of a tracked search."""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.locations import describe_locations
from flighttracker.i18n import translate
from flighttracker.providers.registry import FAKE_DATA_PROVIDERS
from flighttracker.services import history, jobs, price_trends
from flighttracker.services.access import (
    Access,
    search_access,
)
from flighttracker.services.data_export import export_search
from flighttracker.services.searches import (
    spec_of,
)
from flighttracker.services.trips import (
    get_trip_for_search,
)
from flighttracker.web import flights
from flighttracker.web.deps import (
    AuthUser,
    current_locale,
    get_db,
    get_effective_settings,
    require_login,
)
from flighttracker.web.flights import script_json
from flighttracker.web.labels import CABIN_LABELS, CHART_LABELS
from flighttracker.web.routes.searches._common import (
    estimate,
    load_search,
)
from flighttracker.web.sharing import sharing_view
from flighttracker.web.templating import templates

router = APIRouter()


def _ascii_filename(name: str) -> str:
    """Download filenames must be Latin-1 (HTTP header); non-ASCII characters are dropped."""
    ascii_name = name.encode("ascii", "ignore").decode("ascii").strip().lower().replace(" ", "-")
    return ascii_name or "tracked-search"


@router.get("/searches/{search_id}")
def detail(
    search_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
    user: AuthUser = Depends(require_login),
):
    search = load_search(db, search_id, user)
    access = search_access(db, user, search)
    trip = get_trip_for_search(db, search_id)
    spec = spec_of(search)
    today = datetime.now(UTC).date()
    # Only prices for the current passengers: others are not comparable (see services/history).
    passengers = (spec.filters.adults, spec.filters.children)
    points = history.price_points(db, search.id, FAKE_DATA_PROVIDERS, passengers=passengers)
    upcoming = [p for p in points if p.departure_date >= today]
    trends = price_trends.flight_trends(
        upcoming, price_trends.price_series(db, search.id, today, passengers=passengers)
    )
    locale = current_locale(request)
    chart = flights.chart_data(
        points,
        today,
        settings.default_currency,
        {key: translate(text, locale) for key, text in CHART_LABELS.items()},
        detail_url=f"/searches/{search.id}/flights/",
        trends=trends,
        calendar=history.calendar_days(
            db, search.id, today, FAKE_DATA_PROVIDERS, passengers=passengers
        ),
    )
    return templates.TemplateResponse(
        request,
        "searches/detail.html",
        {
            "search": search,
            "trip": trip,
            "spec": spec,
            "origins": describe_locations(spec.origins, spec.country_airports),
            "destinations": describe_locations(spec.destinations, spec.country_airports),
            "estimate": estimate(spec, settings),
            "poll_interval_minutes": settings.default_poll_interval_minutes,
            "points": upcoming,
            "previous_year": flights.previous_year_lookup(points),
            "trend_for": lambda p: trends.get(price_trends.flight_key(p)),
            "has_fake_prices": any(p.fake for p in upcoming),
            "multiple_cabins": len({p.cabin_class for p in upcoming}) > 1,
            "filter_stays": [
                (str(days), str(days))
                for days in sorted({p.stay_days for p in upcoming if p.stay_days is not None})
            ],
            "chart_json": script_json(chart),
            "has_chart_points": bool(chart["points"]),
            "filter_origins": [(code, code) for code in sorted({p.origin for p in upcoming})],
            "filter_destinations": [
                (code, code) for code in sorted({p.destination for p in upcoming})
            ],
            "filter_cabins": [
                (str(c), label)
                for c, label in CABIN_LABELS.items()
                if c in {p.cabin_class for p in upcoming}
            ],
            "passengers": spec.filters.adults + spec.filters.children,
            "summarize": flights.summarize,
            "duration": flights.format_duration,
            "observation_count": history.observation_counts(db, [search.id]).get(search.id, 0),
            "jobs": jobs.recent_jobs(db, search.id),
            "revisions": search.revisions,
            "hidden_passenger_prices": history.other_passenger_prices(
                db, search.id, passengers, today
            ),
            "access": access,
            "Access": Access,
            "sharing": sharing_view(db, user, search.owner_id, search_id=search.id)
            if trip is None and access >= Access.OWN
            else None,
        },
    )


@router.get("/searches/{search_id}/export")
def export_one(
    search_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(require_login)
) -> Response:
    """Download this Suchabo's data (incl. price history and query log) as JSON, importable
    later via the general import button on Platform Settings."""
    search = load_search(db, search_id, user)
    payload = export_search(db, search.id)
    filename = f"plane-and-simple-{_ascii_filename(search.name)}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/searches/{search_id}/flights/{price_id}")
def flight_detail(
    search_id: int,
    price_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    search = load_search(db, search_id, user)
    observed = history.observation(db, search.id, price_id)
    if observed is None:
        raise HTTPException(status_code=404, detail="Price not found.")
    return templates.TemplateResponse(
        request,
        "searches/flight.html",
        {
            "search": search,
            "observed": observed,
            "segments": flights.segments(observed.details),
            "url": (observed.details or {}).get("url"),
            "history": history.flight_history(db, observed),
            "fake": observed.provider in FAKE_DATA_PROVIDERS,
            # The passengers this price was observed for (the Suchabo may have changed since).
            "passengers": observed.adults + observed.children,
            "round_trip": observed.return_date is not None,
        },
    )
