import re

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from flighttracker.models import QueryOutcome
from flighttracker.providers.registry import FAKE_DATA_PROVIDERS
from flighttracker.services import query_log
from flighttracker.web.deps import get_db, require_admin
from flighttracker.web.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])


def _optional_int(raw: str | None) -> int | None:
    # ASCII digits only and within PostgreSQL's bigint range (`isdigit` also accepts "²").
    return int(raw) if raw and re.fullmatch(r"[0-9]{1,18}", raw) else None


@router.get("/log")
def log_page(
    request: Request,
    search_id: str | None = None,
    outcome: str | None = None,
    before: str | None = None,
    db: Session = Depends(get_db),
):
    # Lenient parsing: an empty or invalid filter value simply means "all".
    selected_outcome = outcome if outcome in set(QueryOutcome) else None
    selected_search = _optional_int(search_id)
    page = query_log.list_entries(
        db,
        search_id=selected_search,
        outcome=QueryOutcome(selected_outcome) if selected_outcome else None,
        before=_optional_int(before),
    )
    return templates.TemplateResponse(
        request,
        "log.html",
        {
            "page": page,
            "search_options": [
                (str(search_id), name) for search_id, name in query_log.logged_searches(db)
            ],
            "selected_search": str(selected_search) if selected_search else "",
            "selected_outcome": selected_outcome or "",
            "fake_providers": FAKE_DATA_PROVIDERS,
        },
    )
