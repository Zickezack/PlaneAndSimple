"""Sharing a tracked search and changing its owner."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.i18n import Msg
from flighttracker.models import User
from flighttracker.services.access import (
    Access,
    ShareError,
    remove_share,
    share_with,
)
from flighttracker.services.trips import (
    get_trip_for_search,
)
from flighttracker.web.deps import (
    AuthUser,
    csrf_form,
    flash,
    get_db,
    require_admin,
    require_login,
)
from flighttracker.web.routes.searches._common import (
    load_search,
    redirect,
)

router = APIRouter()


@router.post("/searches/{search_id}/shares")
def add_share(
    search_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    search = load_search(db, search_id, user, Access.OWN)
    if get_trip_for_search(db, search_id) is not None:
        raise HTTPException(status_code=404)
    try:
        share = share_with(
            db,
            owner_id=search.owner_id,
            username=str(form.get("username", "")),
            can_edit=form.get("permission") == "edit",
            search_id=search.id,
        )
    except ShareError as exc:
        flash(request, exc.message, "error")
        return redirect(f"/searches/{search.id}")
    db.commit()
    flash(request, Msg('Shared with "{name}".', name=share.user.username), "success")
    return redirect(f"/searches/{search.id}")


@router.post("/searches/{search_id}/shares/{share_id}/delete")
def delete_share(
    search_id: int,
    share_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    search = load_search(db, search_id, user, Access.OWN)
    if remove_share(db, share_id, search_id=search.id):
        db.commit()
        flash(request, Msg("Access removed."), "success")
    return redirect(f"/searches/{search.id}")


@router.post("/searches/{search_id}/owner", dependencies=[Depends(require_admin)])
def change_owner(
    search_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    search = load_search(db, search_id, user, Access.OWN)
    if get_trip_for_search(db, search_id) is not None:
        raise HTTPException(status_code=404)
    raw = str(form.get("owner_id", ""))
    owner = db.get(User, int(raw)) if raw.isdigit() else None
    if owner is None:
        raise HTTPException(status_code=422, detail="Unknown user.")
    search.owner_id = owner.id
    db.commit()
    flash(request, Msg("Owner changed."), "success")
    return redirect(f"/searches/{search.id}")
