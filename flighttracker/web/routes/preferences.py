from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from flighttracker.i18n import SUPPORTED_LOCALES
from flighttracker.models import User
from flighttracker.web.deps import SESSION_LOCALE, current_user, get_db

router = APIRouter()


@router.get("/language/{locale}")
def set_language(locale: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """Switch the UI language and return to the page the switch was clicked on.

    Logged in, the choice is also kept as the user's personal setting for the next login.
    """
    if locale not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=404)
    request.session[SESSION_LOCALE] = locale
    auth_user = current_user(request)
    if auth_user is not None:
        db.get(User, auth_user.id).locale = locale
        db.commit()
    # Only the path of the referer, so the redirect can never leave this site. Browsers read
    # `//host` and `/\host` as links to another host, so those fall back to the start page.
    referer = urlsplit(request.headers.get("referer", ""))
    path = referer.path
    target = path if path.startswith("/") and not path.startswith(("//", "/\\")) else "/"
    if referer.query:
        target += f"?{referer.query}"
    return RedirectResponse(target, status_code=303)
