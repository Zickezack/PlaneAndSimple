"""'When to book' in sentences: the AI words the rule-based advice of one flight (on request,
from the flight page). The advice and its numbers stay the app's own."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.domain.booking_advice import advise
from flighttracker.domain.currencies import currency_decimals
from flighttracker.i18n import Msg, format_number
from flighttracker.services import ai, ai_prompts, history
from flighttracker.web.deps import (
    AuthUser,
    csrf_form,
    current_locale,
    get_db,
    get_effective_settings,
    require_login,
)
from flighttracker.web.routes.searches._common import load_search
from flighttracker.web.routes.searches.detail import flight_page

router = APIRouter()


@router.post("/searches/{search_id}/flights/{price_id}/explain")
def explain(
    search_id: int,
    price_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
    user: AuthUser = Depends(require_login),
):
    def page(**extra):
        return flight_page(request, db, settings, user, search_id, price_id, **extra)

    search = load_search(db, search_id, user)
    observed = history.observation(db, search.id, price_id)
    if observed is None or not (ai.ai_enabled(settings) and settings.ai_explanations):
        return page()
    rows = list(reversed(history.flight_history(db, observed)))
    advice = advise([row.price for row in rows], observed.departure_date, datetime.now(UTC).date())
    decimals = currency_decimals(observed.currency)

    def money(amount) -> str:
        return f"{format_number(amount, decimals)} {observed.currency}"

    try:
        answer = ai_prompts.explain_advice(
            settings,
            advice,
            route=f"{observed.origin_iata} → {observed.destination_iata}",
            cabin=str(observed.cabin_class).replace("_", " "),
            departure=observed.departure_date,
            return_date=observed.return_date,
            passengers=observed.adults + observed.children,
            prices=[f"{row.observed_at.date().isoformat()}: {money(row.price)}" for row in rows],
            money=money,
            locale=current_locale(request),
            usage=ai.UsageLog(db, "explain", user.id),
        )
    except ai.AIError as exc:
        db.commit()  # the failed request is in the usage log
        return page(explanation_error=Msg("No explanation: {error}", error=str(exc)))
    db.commit()
    return page(explanation=answer.text)
