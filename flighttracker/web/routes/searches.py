import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.domain.coverage import CoverageKind
from flighttracker.domain.locations import describe_locations
from flighttracker.domain.spec import (
    SearchSpec,
    merge_specs,
    queries_per_poll,
    requests_per_query,
    route_pairs,
)
from flighttracker.i18n import Msg, translate
from flighttracker.models import LocationRole, Search, SearchStatus
from flighttracker.providers.registry import FAKE_DATA_PROVIDERS, provider_class
from flighttracker.services import history, jobs
from flighttracker.services.data_transfer import export_search
from flighttracker.services.searches import (
    SearchValidationError,
    airport_options,
    archive_search,
    country_names,
    create_search,
    delete_search_permanently,
    find_overlapping_searches,
    get_search,
    list_searches,
    merge_into,
    pause_search,
    restore_search,
    resume_search,
    spec_of,
    update_search,
    with_default_airports,
)
from flighttracker.services.trips import (
    get_trip_for_search,
    list_trips,
    trip_search_ids,
)
from flighttracker.web import flights
from flighttracker.web.deps import (
    csrf_form,
    current_locale,
    flash,
    get_db,
    get_effective_settings,
    require_admin,
)
from flighttracker.web.forms import (
    SearchFormResult,
    airport_field_name,
    airport_values,
    default_values,
    parse_search_form,
    values_from_search,
)
from flighttracker.web.labels import CABIN_LABELS, CHART_LABELS, DAYS_PER_MONTH_LABELS
from flighttracker.web.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])


def _load(db: Session, search_id: int) -> Search:
    search = get_search(db, search_id)
    if search is None:
        raise HTTPException(status_code=404, detail="Tracked search not found.")
    return search


def _ascii_filename(name: str) -> str:
    """Download filenames must be Latin-1 (HTTP header); non-ASCII characters are dropped."""
    ascii_name = name.encode("ascii", "ignore").decode("ascii").strip().lower().replace(" ", "-")
    return ascii_name or "tracked-search"


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# Rough time per scraper request on top of the configured pause (measured: ~1 s).
_SECONDS_PER_REQUEST = 1.0


def _estimate(spec: SearchSpec, settings: Settings) -> dict:
    """Cost of one poll, so that the user can weigh coverage against requests and time."""
    queries = queries_per_poll(spec)
    samples_days = provider_class(settings.flight_provider).samples_days
    requests = queries * requests_per_query(spec) if samples_days else queries
    seconds = requests * (settings.scraper_request_delay_seconds + _SECONDS_PER_REQUEST)
    return {
        "routes": len(route_pairs(spec)),
        "queries": queries,
        "requests": requests,
        "minutes": max(1, round(seconds / 60)),
        "stay_lengths": spec.filters.stay_lengths,
    }


def _days_per_month_options(current: str) -> list[tuple[str, str]]:
    options = [(str(days), label) for days, label in DAYS_PER_MONTH_LABELS.items()]
    if current and current not in {value for value, _ in options}:
        options.append((current, current))  # e.g. a SCRAPER_DAYS_PER_MONTH outside the presets
    return options


def _render_form(
    request: Request,
    db: Session,
    values: dict,
    *,
    search: Search | None = None,
    spec: SearchSpec | None = None,
    errors: list[str] | None = None,
    overlaps: list[dict] | None = None,
    notice: Msg | None = None,
    status_code: int = 200,
) -> Response:
    countries = set(values["airport_countries"])
    return templates.TemplateResponse(
        request,
        "searches/form.html",
        {
            "values": values,
            "search": search,
            "errors": errors or [],
            "overlaps": overlaps or [],
            "notice": notice,
            "airport_options": airport_options(db, countries),
            "country_names": country_names(db, countries),
            "airport_field_name": airport_field_name,
            "days_per_month_options": _days_per_month_options(values.get("days_per_month", "")),
            "estimate": (
                _estimate(spec, get_effective_settings(request, db)) if spec is not None else None
            ),
        },
        status_code=status_code,
    )


def _review_airports(
    request: Request,
    db: Session,
    settings: Settings,
    parsed: SearchFormResult,
    *,
    search: Search | None = None,
    refresh: bool = False,
) -> Response | None:
    """Show the airport checkboxes (largest airports preselected) before anything is saved.

    Returns None when every country's selection has already been reviewed by the user.
    """
    if not parsed.unreviewed_countries and not refresh:
        return None
    spec = with_default_airports(
        parsed.data.spec,
        airport_options(db, parsed.unreviewed_countries),
        settings.country_default_airports,
    )
    notice = None
    if parsed.unreviewed_countries:
        notice = Msg(
            "The largest airports of each country are preselected. "
            "Please check the selection and save again."
        )
    return _render_form(
        request,
        db,
        parsed.values | airport_values(spec),
        search=search,
        spec=spec,
        notice=notice,
    )


def _overlap_view(new_spec, matches) -> list[dict]:
    """Overlap hints incl. what a merge would change, for the create form."""
    result = []
    for search, kind in matches:
        existing = spec_of(search)
        merged = merge_specs(existing, new_spec)
        result.append(
            {
                "search": search,
                "covered": kind is CoverageKind.COVERED,
                "origins": describe_locations(existing.origins, existing.country_airports),
                "destinations": describe_locations(
                    existing.destinations, existing.country_airports
                ),
                "merged_origins": describe_locations(merged.origins, merged.country_airports),
                "merged_destinations": describe_locations(
                    merged.destinations, merged.country_airports
                ),
                "filters_widened": merged.filters != existing.filters,
            }
        )
    return result


def _location_text(search: Search, role: LocationRole) -> str:
    spec = spec_of(search)
    refs = spec.origins if role == LocationRole.ORIGIN else spec.destinations
    return describe_locations(refs, spec.country_airports)


@router.get("/")
def index() -> Response:
    return _redirect("/searches")


@router.get("/searches")
def search_list(request: Request, archived: bool = False, db: Session = Depends(get_db)):
    all_searches = list_searches(db, archived=archived)
    trips = list_trips(db, archived=archived)
    trip_ids = trip_search_ids(db, trips)
    counts = history.observation_counts(db, [s.id for s in all_searches])
    searches = [search for search in all_searches if search.id not in trip_ids]
    return templates.TemplateResponse(
        request,
        "searches/list.html",
        {
            "searches": searches,
            "trips": trips,
            "counts": counts,
            "archived": archived,
            "location_text": _location_text,
        },
    )


@router.get("/searches/new")
def new_search(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    return _render_form(request, db, default_values(settings))


@router.post("/searches")
def create(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    parsed = parse_search_form(
        form,
        default_currency=settings.default_currency,
        poll_interval_minutes=settings.default_poll_interval_minutes,
    )
    if parsed.data is None:
        return _render_form(request, db, parsed.values, errors=parsed.errors, status_code=422)
    review = _review_airports(
        request, db, settings, parsed, refresh=form.get("refresh_airports") == "1"
    )
    if review is not None:
        return review
    spec = parsed.data.spec
    if form.get("confirm_overlap") != "1":
        matches = find_overlapping_searches(db, spec)
        if matches:
            return _render_form(
                request, db, parsed.values, spec=spec, overlaps=_overlap_view(spec, matches)
            )
    try:
        search = create_search(db, parsed.data, max_route_pairs=settings.max_route_pairs_per_search)
    except SearchValidationError as exc:
        return _render_form(
            request, db, parsed.values, spec=spec, errors=exc.errors, status_code=422
        )
    db.commit()
    flash(
        request,
        Msg('Tracked search "{name}" created. The first poll starts shortly.', name=search.name),
        "success",
    )
    return _redirect(f"/searches/{search.id}")


@router.post("/searches/{search_id}/merge")
def merge(
    search_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    target = _load(db, search_id)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        flash(request, Msg("Trip legs cannot be merged into individually."), "error")
        return _redirect(f"/trips/{trip.id}")
    parsed = parse_search_form(
        form,
        default_currency=settings.default_currency,
        poll_interval_minutes=settings.default_poll_interval_minutes,
    )
    if parsed.data is None:
        return _render_form(request, db, parsed.values, errors=parsed.errors, status_code=422)
    review = _review_airports(request, db, settings, parsed)
    if review is not None:
        return review
    if target.is_archived:
        flash(request, Msg("Archived tracked searches cannot be extended."), "error")
        return _redirect(f"/searches/{target.id}")
    try:
        merge_into(
            db, target, parsed.data.spec, max_route_pairs=settings.max_route_pairs_per_search
        )
    except SearchValidationError as exc:
        db.rollback()
        return _render_form(request, db, parsed.values, errors=exc.errors, status_code=422)
    db.commit()
    flash(
        request,
        Msg('Locations and filters were added to "{name}".', name=target.name),
        "success",
    )
    return _redirect(f"/searches/{target.id}")


def _script_json(data: dict) -> str:
    """JSON that is safe inside <script type="application/json"> (no tag can be closed)."""
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


@router.get("/searches/{search_id}")
def detail(
    search_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    search = _load(db, search_id)
    trip = get_trip_for_search(db, search_id)
    spec = spec_of(search)
    today = datetime.now(UTC).date()
    points = history.price_points(db, search.id, FAKE_DATA_PROVIDERS)
    upcoming = [p for p in points if p.departure_date >= today]
    trends = history.flight_trends(upcoming, history.price_series(db, search.id, today))
    locale = current_locale(request)
    chart = flights.chart_data(
        points,
        today,
        settings.default_currency,
        {key: translate(text, locale) for key, text in CHART_LABELS.items()},
        detail_url=f"/searches/{search.id}/flights/",
        trends=trends,
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
            "estimate": _estimate(spec, settings),
            "poll_interval_minutes": settings.default_poll_interval_minutes,
            "points": upcoming,
            "previous_year": flights.previous_year_lookup(points),
            "trend_for": lambda p: trends.get(history.flight_key(p)),
            "has_fake_prices": any(p.fake for p in upcoming),
            "multiple_cabins": len({p.cabin_class for p in upcoming}) > 1,
            "filter_stays": [
                (str(days), str(days))
                for days in sorted({p.stay_days for p in upcoming if p.stay_days is not None})
            ],
            "chart_json": _script_json(chart),
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
        },
    )


@router.get("/searches/{search_id}/edit")
def edit_form(
    search_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    search = _load(db, search_id)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        return _redirect(f"/trips/{trip.id}")
    return _render_form(
        request, db, values_from_search(search), search=search, spec=spec_of(search)
    )


@router.post("/searches/{search_id}/edit")
def edit(
    search_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    search = _load(db, search_id)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        flash(request, Msg("Trip legs are managed through their Trip."), "error")
        return _redirect(f"/trips/{trip.id}")
    parsed = parse_search_form(
        form,
        default_currency=settings.default_currency,
        poll_interval_minutes=settings.default_poll_interval_minutes,
    )
    if parsed.data is None:
        return _render_form(
            request, db, parsed.values, search=search, errors=parsed.errors, status_code=422
        )
    review = _review_airports(
        request, db, settings, parsed, search=search, refresh=form.get("refresh_airports") == "1"
    )
    if review is not None:
        return review
    try:
        changed = update_search(
            db, search, parsed.data, max_route_pairs=settings.max_route_pairs_per_search
        )
    except SearchValidationError as exc:
        db.rollback()
        return _render_form(
            request,
            db,
            parsed.values,
            search=search,
            spec=parsed.data.spec,
            errors=exc.errors,
            status_code=422,
        )
    db.commit()
    if changed:
        flash(request, Msg("Changes saved. The existing price history is kept."), "success")
    else:
        flash(request, Msg("No changes."))
    return _redirect(f"/searches/{search.id}")


@router.post("/searches/{search_id}/poll")
def poll_now(
    search_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    search = _load(db, search_id)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        if jobs.request_trip_poll(
            db,
            trip,
            datetime.now(UTC),
            poll_interval_minutes=settings.default_poll_interval_minutes,
        ):
            db.commit()
            flash(request, Msg("Trip poll queued."), "success")
        else:
            flash(request, Msg("A poll of this trip is already queued or running."))
        return _redirect(f"/trips/{trip.id}")
    if search.is_archived or search.status is not SearchStatus.ACTIVE:
        flash(request, Msg("Only active tracked searches can be polled."), "error")
    elif jobs.request_poll(
        db,
        search,
        datetime.now(UTC),
        poll_interval_minutes=settings.default_poll_interval_minutes,
    ):
        db.commit()
        flash(request, Msg("Poll started. New prices are added to the history."), "success")
    else:
        flash(request, Msg("A poll of this tracked search is already running."))
    return _redirect(f"/searches/{search.id}")


@router.get("/searches/{search_id}/export")
def export_one(search_id: int, db: Session = Depends(get_db)) -> Response:
    """Download this Suchabo's data (incl. price history and query log) as JSON, importable
    later via the general import button on Platform Settings."""
    search = _load(db, search_id)
    payload = export_search(db, search.id)
    filename = f"plane-and-simple-{_ascii_filename(search.name)}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/searches/{search_id}/flights/{price_id}")
def flight_detail(search_id: int, price_id: int, request: Request, db: Session = Depends(get_db)):
    search = _load(db, search_id)
    observed = history.observation(db, search.id, price_id)
    if observed is None:
        raise HTTPException(status_code=404, detail="Price not found.")
    spec = spec_of(search)
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
            "passengers": spec.filters.adults + spec.filters.children,
            "round_trip": observed.return_date is not None,
        },
    )


_ACTIONS = {
    "pause": (pause_search, Msg("Tracked search paused.")),
    "resume": (resume_search, Msg("Tracked search is polled again.")),
    "archive": (archive_search, Msg("Tracked search archived. All data is kept.")),
    "restore": (restore_search, Msg("Tracked search restored.")),
}


@router.post("/searches/{search_id}/delete")
def delete_permanently(
    search_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
):
    search = _load(db, search_id)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        flash(request, Msg("Trip legs cannot be deleted individually."), "error")
        return _redirect(f"/trips/{trip.id}")
    if str(form.get("confirm_name", "")).strip() != search.name:
        flash(request, Msg("To delete permanently, please enter the exact name."), "error")
        return _redirect(f"/searches/{search.id}")
    name = search.name
    delete_search_permanently(db, search)
    db.commit()
    flash(
        request,
        Msg('Tracked search "{name}" and its price history were deleted permanently.', name=name),
        "success",
    )
    return _redirect("/searches")


@router.post("/searches/{search_id}/{action}")
def status_action(
    search_id: int,
    action: str,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
):
    if action not in _ACTIONS:
        raise HTTPException(status_code=404)
    search = _load(db, search_id)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        flash(request, Msg("Trip legs are managed through their Trip."), "error")
        return _redirect(f"/trips/{trip.id}")
    handler, message = _ACTIONS[action]
    handler(search)
    db.commit()
    flash(request, message, "success")
    return _redirect(f"/searches/{search.id}")
