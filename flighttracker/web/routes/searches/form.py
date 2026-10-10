"""Create, edit and merge forms of tracked searches (airport review, overlap hints, quota)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.domain.coverage import CoverageKind
from flighttracker.domain.locations import describe_locations
from flighttracker.domain.spec import (
    SearchSpec,
    merge_specs,
)
from flighttracker.i18n import Msg
from flighttracker.models import Search, User
from flighttracker.services import quota
from flighttracker.services.access import (
    Access,
    search_access,
    visible_searches,
)
from flighttracker.services.locations import airport_options, country_names, with_default_airports
from flighttracker.services.searches import (
    SearchValidationError,
    create_search,
    find_overlapping_searches,
    merge_into,
    spec_of,
    update_search,
)
from flighttracker.services.trips import (
    get_trip_for_search,
)
from flighttracker.web.deps import (
    AuthUser,
    csrf_form,
    current_locale,
    current_user,
    flash,
    get_db,
    get_effective_settings,
    require_login,
)
from flighttracker.web.forms import (
    SearchFormResult,
    airport_field_name,
    airport_values,
    default_values,
    parse_search_form,
    values_from_search,
)
from flighttracker.web.labels import DAYS_PER_MONTH_LABELS
from flighttracker.web.routes.searches._common import (
    added_requests,
    check_quota,
    estimate,
    load_search,
    redirect,
)
from flighttracker.web.templating import templates


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
    settings = get_effective_settings(request, db)
    auth_user = current_user(request)
    usage = quota.usage(db, db.get(User, auth_user.id), settings) if auth_user else None
    return templates.TemplateResponse(
        request,
        "searches/form.html",
        {
            "usage": usage,
            "values": values,
            "search": search,
            "errors": errors or [],
            "overlaps": overlaps or [],
            "notice": notice,
            "airport_options": airport_options(db, countries),
            "country_names": country_names(db, countries, current_locale(request)),
            "airport_field_name": airport_field_name,
            "days_per_month_options": _days_per_month_options(values.get("days_per_month", "")),
            "estimate": estimate(spec, settings) if spec is not None else None,
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


def _overlap_view(db: Session, user: AuthUser, new_spec, matches) -> list[dict]:
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
                "can_merge": search_access(db, user, search) >= Access.EDIT,
            }
        )
    return result


router = APIRouter()


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
    user: AuthUser = Depends(require_login),
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
        matches = find_overlapping_searches(db, spec, visible=visible_searches(user))
        if matches:
            return _render_form(
                request,
                db,
                parsed.values,
                spec=spec,
                overlaps=_overlap_view(db, user, spec, matches),
            )
    quota_errors = check_quota(
        db,
        settings,
        user.id,
        added_searches=1,
        added_requests=quota.search_requests(spec, settings),
    )
    if quota_errors:
        return _render_form(
            request, db, parsed.values, spec=spec, errors=quota_errors, status_code=422
        )
    try:
        search = create_search(
            db, parsed.data, max_route_pairs=settings.max_route_pairs_per_search, owner_id=user.id
        )
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
    return redirect(f"/searches/{search.id}")


@router.post("/searches/{search_id}/merge")
def merge(
    search_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
    user: AuthUser = Depends(require_login),
):
    target = load_search(db, search_id, user, Access.EDIT)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        flash(request, Msg("Trip legs cannot be merged into individually."), "error")
        return redirect(f"/trips/{trip.id}")
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
        return redirect(f"/searches/{target.id}")
    merged = merge_specs(spec_of(target), parsed.data.spec)
    quota_errors = check_quota(
        db, settings, target.owner_id, added_requests=added_requests(target, merged, settings)
    )
    if quota_errors:
        return _render_form(request, db, parsed.values, errors=quota_errors, status_code=422)
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
    return redirect(f"/searches/{target.id}")


@router.get("/searches/{search_id}/edit")
def edit_form(
    search_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    search = load_search(db, search_id, user, Access.EDIT)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        return redirect(f"/trips/{trip.id}")
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
    user: AuthUser = Depends(require_login),
):
    search = load_search(db, search_id, user, Access.EDIT)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        flash(request, Msg("Trip legs are managed through their Trip."), "error")
        return redirect(f"/trips/{trip.id}")
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
    quota_errors = check_quota(
        db,
        settings,
        search.owner_id,
        added_requests=added_requests(search, parsed.data.spec, settings),
    )
    if quota_errors:
        return _render_form(
            request,
            db,
            parsed.values,
            search=search,
            spec=parsed.data.spec,
            errors=quota_errors,
            status_code=422,
        )
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
    return redirect(f"/searches/{search.id}")
