"""Routes of tracked searches (Suchabos), one module per sub-feature.

The order matters: FastAPI matches routes in registration order. `form` registers
GET /searches/new before `detail` registers GET /searches/{search_id}, and `actions` ends
with the catch-all POST /searches/{search_id}/{action}, so it must be included last.
"""

from fastapi import APIRouter, Depends

from flighttracker.web.deps import require_login
from flighttracker.web.routes.searches import actions, detail, form, listing, shares

router = APIRouter(dependencies=[Depends(require_login)])
for module in (listing, form, detail, shares, actions):
    router.include_router(module.router)
