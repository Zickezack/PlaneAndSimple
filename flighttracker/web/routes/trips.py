from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.i18n import Msg
from flighttracker.models import User
from flighttracker.providers.registry import FAKE_DATA_PROVIDERS, provider_class
from flighttracker.services import quota
from flighttracker.services.access import (
    Access,
    ShareError,
    remove_share,
    set_trip_owner,
    share_with,
    trip_access,
)
from flighttracker.services.jobs import request_trip_poll
from flighttracker.services.locations import airport_options, country_names
from flighttracker.services.searches import SearchValidationError
from flighttracker.services.settings import effective_settings
from flighttracker.services.trips import (
    TripValidationError,
    create_trip,
    estimated_requests,
    get_trip,
    itineraries_for_trip,
    validate_trip,
)
from flighttracker.web.deps import (
    AuthUser,
    csrf_form,
    current_locale,
    current_user,
    flash,
    get_db,
    require_admin,
    require_login,
)
from flighttracker.web.sharing import sharing_view
from flighttracker.web.templating import templates
from flighttracker.web.trip_forms import (
    default_trip_values,
    review_country_airports,
    trip_airport_field_name,
    trip_input_from_values,
    trip_values_from_form,
)

router = APIRouter(dependencies=[Depends(require_login)])


def _render_form(request: Request, db: Session, values: dict, errors: list, notice=None):
    countries = {code for leg in values["legs"] for code in leg["airport_countries"]}
    auth_user = current_user(request)
    settings = effective_settings(db, request.app.state.settings)
    return templates.TemplateResponse(
        request,
        "trips/form.html",
        {
            "usage": quota.usage(db, db.get(User, auth_user.id), settings),
            "values": values,
            "errors": errors,
            "notice": notice,
            "airport_options": airport_options(db, countries),
            "country_names": country_names(db, countries, current_locale(request)),
            "trip_airport_field_name": trip_airport_field_name,
        },
    )


def _load_trip(db: Session, trip_id: int, user: AuthUser, needed: Access = Access.VIEW):
    trip = get_trip(db, trip_id)
    access = trip_access(db, user, trip) if trip is not None else None
    if access is None:
        raise HTTPException(status_code=404, detail="Trip not found.")
    if access < needed:
        raise HTTPException(status_code=403, detail="You may not change this Trip.")
    return trip


@router.get("/trips/new")
def new_trip(request: Request, db: Session = Depends(get_db)):
    settings = effective_settings(db, request.app.state.settings)
    return _render_form(request, db, default_trip_values(settings), [])


@router.post("/trips")
def create(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    settings = effective_settings(db, request.app.state.settings)
    values = default_trip_values(settings)
    try:
        values = trip_values_from_form(form, settings)
        if review_country_airports(db, settings, values):
            return _render_form(
                request,
                db,
                values,
                [],
                Msg(
                    "The largest airports of each country are preselected. "
                    "Please check the selection and create the trip again."
                ),
            )
        data = trip_input_from_values(values, settings)
        validate_trip(data, date.today())
    except (ValueError, TypeError) as exc:
        return _render_form(request, db, values, [str(exc)])
    provider = provider_class(settings.flight_provider)
    if not provider.supports_connection_times:
        return _render_form(
            request,
            db,
            values,
            [
                "The Trip Planner needs a provider with flight times; select Google Flights "
                "in Platform Settings."
            ],
        )
    quota_errors = quota.check(
        db,
        db.get(User, user.id),
        settings,
        added_searches=1,
        added_requests=estimated_requests(data),
    )
    if quota_errors:
        return _render_form(request, db, values, quota_errors)
    try:
        trip = create_trip(
            db, data, max_route_pairs=settings.max_route_pairs_per_search, owner_id=user.id
        )
    except TripValidationError as exc:
        db.rollback()
        return _render_form(request, db, values, [str(exc)])
    except SearchValidationError as exc:
        db.rollback()
        return _render_form(request, db, values, exc.errors)
    db.commit()
    flash(
        request,
        Msg('Trip "{name}" created. Its first coordinated poll starts shortly.', name=trip.name),
        "success",
    )
    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


@router.get("/trips/{trip_id}")
def detail(
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
) -> Response:
    trip = _load_trip(db, trip_id, user)
    access = trip_access(db, user, trip)
    settings = effective_settings(db, request.app.state.settings)
    return templates.TemplateResponse(
        request,
        "trips/detail.html",
        {
            "trip": trip,
            "itineraries": itineraries_for_trip(db, trip, FAKE_DATA_PROVIDERS),
            "poll_interval_minutes": settings.default_poll_interval_minutes,
            "access": access,
            "Access": Access,
            "sharing": sharing_view(db, user, trip.owner_id, trip_id=trip.id)
            if access >= Access.OWN
            else None,
        },
    )


@router.post("/trips/{trip_id}/poll")
def poll_now(
    trip_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    trip = _load_trip(db, trip_id, user, Access.EDIT)
    settings = effective_settings(db, request.app.state.settings)
    if request_trip_poll(
        db,
        trip,
        datetime.now(UTC),
        poll_interval_minutes=settings.default_poll_interval_minutes,
    ):
        db.commit()
        flash(request, Msg("Trip poll queued."), "success")
    else:
        flash(request, Msg("A poll of this trip is already queued or running."))
    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


@router.post("/trips/{trip_id}/shares")
def add_share(
    trip_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    trip = _load_trip(db, trip_id, user, Access.OWN)
    try:
        share = share_with(
            db,
            owner_id=trip.owner_id,
            username=str(form.get("username", "")),
            can_edit=form.get("permission") == "edit",
            trip_id=trip.id,
        )
    except ShareError as exc:
        flash(request, exc.message, "error")
        return RedirectResponse(f"/trips/{trip.id}", status_code=303)
    db.commit()
    flash(request, Msg('Shared with "{name}".', name=share.user.username), "success")
    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


@router.post("/trips/{trip_id}/shares/{share_id}/delete")
def delete_share(
    trip_id: int,
    share_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    trip = _load_trip(db, trip_id, user, Access.OWN)
    if remove_share(db, share_id, trip_id=trip.id):
        db.commit()
        flash(request, Msg("Access removed."), "success")
    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


@router.post("/trips/{trip_id}/owner", dependencies=[Depends(require_admin)])
def change_owner(
    trip_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    trip = _load_trip(db, trip_id, user, Access.OWN)
    raw = str(form.get("owner_id", ""))
    owner = db.get(User, int(raw)) if raw.isdigit() else None
    if owner is None:
        raise HTTPException(status_code=422, detail="Unknown user.")
    set_trip_owner(trip, owner.id)
    db.commit()
    flash(request, Msg("Owner changed."), "success")
    return RedirectResponse(f"/trips/{trip.id}", status_code=303)
