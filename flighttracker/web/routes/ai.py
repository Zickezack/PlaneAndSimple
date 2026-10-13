"""AI features in the web: connection test (Platform Settings), tracked-search draft from a
plain-language wish, and the "when to book" advice in sentences on the flight page."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.i18n import Msg
from flighttracker.services import ai, ai_prompts
from flighttracker.web.deps import csrf_form, flash, get_effective_settings, require_admin

router = APIRouter()


@router.post("/settings/ai/test", dependencies=[Depends(require_admin)])
def test_connection(
    request: Request,
    form: FormData = Depends(csrf_form),
    settings: Settings = Depends(get_effective_settings),
):
    try:
        answer = ai_prompts.connection_test(settings)
    except ai.AIError as exc:
        flash(request, Msg("AI not reachable: {error}", error=str(exc)), "error")
    else:
        flash(
            request,
            Msg(
                "AI answered in {seconds} s: {answer}",
                seconds=f"{answer.seconds:.1f}",
                answer=answer.text[:80],
            ),
            "success",
        )
    return RedirectResponse("/settings#ai", status_code=303)
