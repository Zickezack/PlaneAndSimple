"""Quick navigation and collapsible sections on long pages (Settings, tracked search)."""

from tests.integration.test_services import create
from tests.integration.test_users import add_user, login_as
from tests.integration.test_web import client, csrf, login  # noqa: F401 (fixture)


def test_settings_navigation_for_admins_only(client, db):  # noqa: F811
    add_user(db, "alice")
    login_as(client, "alice")
    page = client.get("/settings").text
    assert "data-section-nav" not in page  # two sections only
    assert '<details class="card more" id="personal" open>' in page

    login(client)
    page = client.get("/settings").text
    for anchor in ("personal", "password", "schedule", "transfer", "providers", "users"):
        assert f'<a href="#{anchor}">' in page
        assert f'id="{anchor}"' in page


def test_saving_a_section_returns_to_it(client, db):  # noqa: F811
    login(client)
    token = csrf(client.get("/settings").text)
    response = client.post(
        "/settings/section/worker",
        data={"csrf_token": token, "worker_tick_seconds": "30", "max_route_pairs_per_search": "50"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings#worker"


def test_tracked_search_navigation(client, db):  # noqa: F811
    search = create(db)
    db.commit()
    login(client)
    page = client.get(f"/searches/{search.id}").text
    for anchor in ("prices", "search-settings", "polls", "history", "sharing", "danger-zone"):
        assert f'<a href="#{anchor}">' in page
        assert f'id="{anchor}"' in page
