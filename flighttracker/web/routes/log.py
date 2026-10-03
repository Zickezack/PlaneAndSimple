import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, true
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.i18n import Msg
from flighttracker.models import FetchJob, QueryOutcome, Search
from flighttracker.providers.registry import FAKE_DATA_PROVIDERS
from flighttracker.services import jobs, query_log
from flighttracker.services.access import visible_searches
from flighttracker.web.deps import AuthUser, csrf_form, flash, get_db, require_login
from flighttracker.web.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])


def _optional_int(raw: str | None) -> int | None:
    # ASCII digits only and within PostgreSQL's bigint range (`isdigit` also accepts "²").
    return int(raw) if raw and re.fullmatch(r"[0-9]{1,18}", raw) else None


def _cancellable(db: Session, user: AuthUser, job_ids: list[int]) -> set[int]:
    """Jobs of Suchabos (Trip legs: of Trips) that `user` may edit."""
    editable = visible_searches(user, edit_only=True)
    return set(
        db.scalars(
            select(FetchJob.id)
            .join(Search, Search.id == FetchJob.search_id)
            .where(FetchJob.id.in_(job_ids), editable if editable is not None else true())
        )
    )


@router.get("/log")
def log_page(
    request: Request,
    search_id: str | None = None,
    outcome: str | None = None,
    before: str | None = None,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    # Lenient parsing: an empty or invalid filter value simply means "all".
    selected_outcome = outcome if outcome in set(QueryOutcome) else None
    selected_search = _optional_int(search_id)
    visible = visible_searches(user)
    page = query_log.list_entries(
        db,
        search_id=selected_search,
        outcome=QueryOutcome(selected_outcome) if selected_outcome else None,
        before=_optional_int(before),
        visible=visible,
    )
    open_jobs = jobs.open_jobs(db, visible)
    return templates.TemplateResponse(
        request,
        "log.html",
        {
            "page": page,
            "open_jobs": open_jobs,
            "cancellable": _cancellable(db, user, [o.job.id for o in open_jobs]),
            "search_options": [
                (str(search_id), name) for search_id, name in query_log.logged_searches(db, visible)
            ],
            "selected_search": str(selected_search) if selected_search else "",
            "selected_outcome": selected_outcome or "",
            "fake_providers": FAKE_DATA_PROVIDERS,
        },
    )


@router.post("/log/jobs/{job_id}/cancel")
def cancel_job(
    job_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    if _cancellable(db, user, [job_id]) and jobs.cancel_job(db, job_id, datetime.now(UTC)):
        db.commit()
        flash(
            request,
            Msg("Poll cancelled. A running poll stops after its current query."),
            "success",
        )
    else:
        flash(request, Msg("This poll is no longer queued or running."))
    return RedirectResponse("/log", status_code=303)
