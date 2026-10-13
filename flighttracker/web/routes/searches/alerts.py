"""Price alerts of a tracked search: every user who can see it may set their own (by mail)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.domain.spec import route_pairs
from flighttracker.i18n import Msg
from flighttracker.models import Search, User
from flighttracker.services import mail, price_alerts
from flighttracker.services.searches import spec_of
from flighttracker.services.trips import get_trip_for_search
from flighttracker.web.deps import (
    AuthUser,
    csrf_form,
    flash,
    get_db,
    get_effective_settings,
    get_user,
    require_login,
)
from flighttracker.web.labels import CABIN_LABELS
from flighttracker.web.routes.searches._common import load_search, redirect

router = APIRouter()


def alerts_view(db: Session, search: Search, user: User, settings: Settings) -> dict:
    """The "Price alerts" section of the detail page."""
    spec = spec_of(search)
    return {
        "items": price_alerts.alerts_of(db, search.id, user.id),
        "has_email": bool(user.email),
        "mail_enabled": mail.mail_enabled(settings),
        "currency": settings.default_currency,
        "routes": [(f"{o}-{d}", f"{o} → {d}") for o, d in route_pairs(spec)],
        "cabins": [(str(c), CABIN_LABELS[c]) for c in sorted(spec.filters.cabin_classes)],
    }


def _back(search_id: int):
    return redirect(f"/searches/{search_id}#alerts")


@router.post("/searches/{search_id}/alerts")
def add_alert(
    search_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
    auth: AuthUser = Depends(require_login),
    user: User = Depends(get_user),
):
    search = load_search(db, search_id, auth)
    if get_trip_for_search(db, search_id) is not None:
        flash(request, Msg("Trip legs are managed through their Trip."), "error")
        return _back(search_id)
    try:
        price_alerts.create_alert(
            db,
            search,
            user,
            max_price=str(form.get("max_price", "")),
            currency=settings.default_currency,
            route=str(form.get("route", "")),
            cabin=str(form.get("cabin", "")),
        )
    except price_alerts.AlertValidationError as exc:
        db.rollback()
        for error in exc.errors:
            flash(request, error, "error")
        return _back(search_id)
    db.commit()
    flash(
        request,
        Msg("Price alert saved. You get a mail after the next poll that finds such a price."),
        "success",
    )
    return _back(search_id)


@router.post("/searches/{search_id}/alerts/{alert_id}/delete")
def delete_alert(
    search_id: int,
    alert_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    load_search(db, search_id, user)
    if price_alerts.delete_alert(db, search_id, alert_id, user.id):
        db.commit()
        flash(request, Msg("Price alert deleted."), "success")
    return _back(search_id)
