from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from flighttracker.i18n import SUPPORTED_LOCALES
from flighttracker.web.deps import SESSION_LOCALE

router = APIRouter()


@router.get("/language/{locale}")
def set_language(locale: str, request: Request) -> RedirectResponse:
    """Switch the UI language and return to the page the switch was clicked on."""
    if locale not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=404)
    request.session[SESSION_LOCALE] = locale
    # Only the path of the referer, so the redirect can never leave this site. Browsers read
    # `//host` and `/\host` as links to another host, so those fall back to the start page.
    referer = urlsplit(request.headers.get("referer", ""))
    path = referer.path
    target = path if path.startswith("/") and not path.startswith(("//", "/\\")) else "/"
    if referer.query:
        target += f"?{referer.query}"
    return RedirectResponse(target, status_code=303)
