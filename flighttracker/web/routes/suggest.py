"""JSON suggestions for the autocomplete fields of the Suchabo form."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from flighttracker.domain.currencies import suggest_currencies
from flighttracker.i18n import translate
from flighttracker.services.searches import suggest_locations
from flighttracker.web.deps import current_locale, get_db, require_admin
from flighttracker.web.labels import LOCATION_KIND_LABELS

router = APIRouter(prefix="/api/suggest", dependencies=[Depends(require_admin)])

MAX_QUERY_LENGTH = 50


@router.get("/locations")
def locations(request: Request, q: str = "", db: Session = Depends(get_db)) -> list[dict]:
    locale = current_locale(request)
    return [
        {
            "value": s.code,
            "label": s.label,
            "kind": translate(LOCATION_KIND_LABELS[s.kind], locale),
        }
        for s in suggest_locations(db, q[:MAX_QUERY_LENGTH])
    ]


@router.get("/currencies")
def currencies(request: Request, q: str = "") -> list[dict]:
    locale = current_locale(request)
    return [
        {"value": code, "label": translate(name, locale), "kind": ""}
        for code, name in suggest_currencies(q[:MAX_QUERY_LENGTH])
    ]
