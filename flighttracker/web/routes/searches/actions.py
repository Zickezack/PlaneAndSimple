"""Poll now, pause/resume/archive/restore and permanent deletion.

`status_action` is a catch-all (`/searches/{id}/{action}`) and must stay the last route.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.i18n import Msg
from flighttracker.models import SearchStatus
from flighttracker.services import jobs, quota
from flighttracker.services.access import (
    Access,
)
from flighttracker.services.searches import (
    archive_search,
    delete_search_permanently,
    pause_search,
    restore_search,
    resume_search,
    spec_of,
)
from flighttracker.services.trips import (
    get_trip_for_search,
)
from flighttracker.web.deps import (
    AuthUser,
    csrf_form,
    flash,
    get_db,
    get_effective_settings,
    require_login,
)
from flighttracker.web.routes.searches._common import (
    check_quota,
    load_search,
    redirect,
)

router = APIRouter()


@router.post("/searches/{search_id}/poll")
def poll_now(
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
        return redirect(f"/trips/{trip.id}")
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
    return redirect(f"/searches/{search.id}")


_ACTIONS = {
    "pause": (pause_search, Msg("Tracked search paused."), Access.EDIT),
    "resume": (resume_search, Msg("Tracked search is polled again."), Access.EDIT),
    "archive": (archive_search, Msg("Tracked search archived. All data is kept."), Access.OWN),
    "restore": (restore_search, Msg("Tracked search restored."), Access.OWN),
}


@router.post("/searches/{search_id}/delete")
def delete_permanently(
    search_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    search = load_search(db, search_id, user, Access.OWN)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        flash(request, Msg("Trip legs cannot be deleted individually."), "error")
        return redirect(f"/trips/{trip.id}")
    if str(form.get("confirm_name", "")).strip() != search.name:
        flash(request, Msg("To delete permanently, please enter the exact name."), "error")
        return redirect(f"/searches/{search.id}")
    name = search.name
    delete_search_permanently(db, search)
    db.commit()
    flash(
        request,
        Msg('Tracked search "{name}" and its price history were deleted permanently.', name=name),
        "success",
    )
    return redirect("/searches")


@router.post("/searches/{search_id}/{action}")
def status_action(
    search_id: int,
    action: str,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
    user: AuthUser = Depends(require_login),
):
    if action not in _ACTIONS:
        raise HTTPException(status_code=404)
    handler, message, needed = _ACTIONS[action]
    search = load_search(db, search_id, user, needed)
    trip = get_trip_for_search(db, search_id)
    if trip is not None:
        flash(request, Msg("Trip legs are managed through their Trip."), "error")
        return redirect(f"/trips/{trip.id}")
    if action == "restore" and search.is_archived:
        errors = check_quota(
            db,
            settings,
            search.owner_id,
            added_searches=1,
            added_requests=quota.search_requests(spec_of(search), settings),
        )
        if errors:
            for error in errors:
                flash(request, error, "error")
            return redirect(f"/searches/{search.id}")
    handler(search)
    db.commit()
    flash(request, message, "success")
    return redirect(f"/searches/{search.id}")
