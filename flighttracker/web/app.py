from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from flighttracker.config import Settings, get_settings
from flighttracker.db.session import create_db_engine, create_session_factory
from flighttracker.security.throttle import LoginThrottle
from flighttracker.web.deps import LoginRequired
from flighttracker.web.routes import (
    auth,
    health,
    log,
    preferences,
    searches,
    suggest,
    trips,
    users,
)
from flighttracker.web.routes import (
    settings as settings_routes,
)
from flighttracker.web.templating import STATIC_DIR

SESSION_MAX_AGE_SECONDS = 12 * 60 * 60

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if session_factory is None:
        session_factory = create_session_factory(create_db_engine(settings.database_url))

    # No public OpenAPI/docs: the app is HTML-only and may be hosted publicly.
    app = FastAPI(title="Plane and simple", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.login_throttle = LoginThrottle()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        session_cookie="pas_session",
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=settings.session_cookie_secure,
    )

    @app.exception_handler(LoginRequired)
    async def redirect_to_login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(preferences.router)
    app.include_router(suggest.router)
    app.include_router(searches.router)
    app.include_router(log.router)
    app.include_router(settings_routes.router)
    app.include_router(trips.router)
    app.include_router(users.router)
    return app
