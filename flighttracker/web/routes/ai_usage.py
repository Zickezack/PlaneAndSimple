"""One request of the AI usage log in detail (admins only), stage by stage: what the app sent,
the thinking the provider returned, what came back and what the app made of it."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from flighttracker.services import usage_logs
from flighttracker.services.ai_log import MESSAGE_COLUMNS
from flighttracker.web import labels
from flighttracker.web.deps import get_db, require_admin
from flighttracker.web.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])

RELOADED_SECONDS = 1.0  # loading took longer: the model was (re)loaded for this request


def _pretty(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) if value else ""


def _timings(response: dict | None) -> dict[str, float]:
    """Ollama's durations (nanoseconds in its response) in seconds: what it spent the time on."""
    found = response or {}
    return {
        key: found[key] / 1e9
        for key in labels.AI_TIMING_LABELS
        if isinstance(found.get(key), int | float)
    }


@router.get("/settings/ai-usage/{entry_id}")
def ai_request(entry_id: int, request: Request, db: Session = Depends(get_db)):
    row = usage_logs.ai_entry(db, entry_id)
    if row is None:
        raise HTTPException(status_code=404)
    entry = row.entry
    timings = _timings(entry.response)
    return templates.TemplateResponse(
        request,
        "settings/ai_request.html",
        {
            "row": row,
            "params": _pretty((entry.request or {}).get("body")),
            "response": _pretty(entry.response),
            "timings": timings,
            "reloaded": timings.get("load_duration", 0) > RELOADED_SECONDS,
            "has_messages": any(getattr(entry, column) for column in MESSAGE_COLUMNS),
        },
    )
