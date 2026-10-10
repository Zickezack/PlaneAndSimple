"""FastAPI matches routes in registration order; the searches package must keep it."""

from flighttracker.web.routes.searches import router


def _routes(routes):
    for route in routes:
        included = getattr(route, "original_router", None)  # include_router() wrapper
        yield from _routes(included.routes) if included else [route]


def _paths(method: str) -> list[str]:
    return [r.path for r in _routes(router.routes) if method in getattr(r, "methods", set())]


def test_new_form_comes_before_the_detail_page():
    gets = _paths("GET")
    assert gets.index("/searches/new") < gets.index("/searches/{search_id}")


def test_status_catch_all_is_the_last_post_route():
    assert _paths("POST")[-1] == "/searches/{search_id}/{action}"
