"""Tracked-search draft from a plain-language travel wish (AI). The draft only fills the form;
the user checks it and saves it through the normal form, with all its validation."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.i18n import Msg
from flighttracker.services import ai, ai_prompts
from flighttracker.services.locations import suggest_locations
from flighttracker.web.deps import (
    csrf_form,
    current_locale,
    current_user,
    get_db,
    get_effective_settings,
)
from flighttracker.web.forms import default_values
from flighttracker.web.routes.searches.form import render_form

router = APIRouter()


def _resolve(db: Session, names: list[str], locale: str) -> tuple[list[str], list[str]]:
    """Codes for place names the model wrote out ("Lisbon" → LIS); the rest stays unknown."""
    codes, unknown = [], []
    for name in names:
        found = suggest_locations(db, name, limit=1, locale=locale)
        if found:
            codes.append(found[0].code)
        else:
            unknown.append(name)
    return codes, unknown


@router.post("/searches/draft")
def draft(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    wish = str(form.get("wish", "")).strip()
    defaults = default_values(settings)
    if not (ai.ai_enabled(settings) and settings.ai_drafts):
        return render_form(request, db, defaults, errors=[Msg("AI drafts are switched off.")])
    if not wish:
        return render_form(request, db, defaults, errors=[Msg("Describe your trip first.")])
    locale = current_locale(request)
    usage = ai.UsageLog(db, "draft", current_user(request).id)
    try:
        result = ai_prompts.draft_search(
            settings, wish, datetime.now(UTC).date(), locale, usage=usage
        )
    except ai.AIError as exc:
        db.commit()  # the failed request is in the usage log
        return render_form(
            request, db, defaults, wish=wish, errors=[Msg("No draft: {error}", error=str(exc))]
        )
    db.commit()
    values, unresolved = ai_prompts.draft_values(result, defaults)
    unknown: list[str] = []
    for key, names in unresolved.items():
        codes, missing = _resolve(db, names, locale)
        existing = [c for c in values[key].split(", ") if c]
        values[key] = ", ".join(dict.fromkeys(existing + codes))
        unknown += missing
    errors = (
        [Msg("Not found, please add by hand: {names}", names=", ".join(unknown))] if unknown else []
    )
    notice = Msg(
        "Draft from your description – check every field, then create the tracked search. {notes}",
        notes=str(result.get("notes") or "")[:300],
    )
    return render_form(request, db, values, notice=notice, errors=errors, wish=wish)
