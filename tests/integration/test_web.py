import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from flighttracker.config import Settings
from flighttracker.domain.filters import CabinClass
from flighttracker.models import (
    PriceHistory,
    PriceSource,
    QueryLog,
    QueryOutcome,
    Search,
    SearchRevision,
    Trip,
)
from flighttracker.security.passwords import hash_password
from flighttracker.web.app import create_app

PASSWORD = "a-very-long-test-password"


def make_client(session_factory, provider="mock") -> TestClient:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused/unused_test",
        secret_key="x" * 40,
        admin_username="admin",
        admin_password_hash=hash_password(PASSWORD, n=2**10),
        session_cookie_secure=False,
        flight_provider=provider,
    )
    return TestClient(create_app(settings, session_factory))


@pytest.fixture
def client(db, session_factory):
    with make_client(session_factory) as test_client:
        yield test_client


def csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client) -> str:
    token = csrf(client.get("/login").text)
    response = client.post(
        "/login",
        data={"csrf_token": token, "username": "admin", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf(client.get("/searches").text)


def search_form(token, **overrides):
    return {
        "csrf_token": token,
        "name": "Zürich → Barcelona",
        "origins": "ZRH",
        "destinations": "BCN",
        "trip_type": "round_trip",
        "cabin_classes": ["economy"],
        "max_stops": "any",
        "months_ahead": "3",
        "adults": "1",
        "currency": "CHF",
        "poll_interval_hours": "6",
    } | overrides


def test_pages_require_login(client):
    response = client.get("/searches", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_security_headers(client):
    response = client.get("/login")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_login_rejects_wrong_password_and_missing_csrf(client):
    token = csrf(client.get("/login").text)
    wrong = client.post(
        "/login", data={"csrf_token": token, "username": "admin", "password": "nope"}
    )
    assert wrong.status_code == 401
    no_csrf = client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert no_csrf.status_code == 403


def test_login_lockout(client):
    token = csrf(client.get("/login").text)
    for _ in range(5):
        client.post("/login", data={"csrf_token": token, "username": "x", "password": "y"})
    locked = client.post(
        "/login", data={"csrf_token": token, "username": "admin", "password": PASSWORD}
    )
    assert locked.status_code == 429


def test_create_search_with_overlap_hint_and_detail(client):
    token = login(client)
    country_form = search_form(token, name="Schweiz → Spanien", origins="CH", destinations="ES")

    # Countries first show their airports (largest preselected) instead of saving right away.
    review = client.post("/searches", data=country_form, follow_redirects=False)
    assert review.status_code == 200
    assert "The largest airports of each country are preselected" in review.text
    assert 'name="airports_CH" value="ZRH"' in review.text
    assert 'name="airport_countries" value="CH,ES"' in review.text

    created = client.post(
        "/searches",
        data=country_form
        | {
            "airport_countries": "CH,ES",
            "airports_CH": ["ZRH", "GVA"],
            "airports_ES": ["BCN", "MAD", "PMI"],
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    detail_url = created.headers["location"]

    detail = client.get(detail_url)
    assert detail.status_code == 200
    assert "Schweiz → Spanien" in detail.text
    assert "CH (GVA, ZRH)" in detail.text
    assert "6 routes · 18 queries · 18 requests" in detail.text
    assert "No prices yet" in detail.text

    hint = client.post("/searches", data=search_form(token))
    assert hint.status_code == 200
    assert "Already covered" in hint.text

    forced = client.post(
        "/searches", data=search_form(token, confirm_overlap="1"), follow_redirects=False
    )
    assert forced.status_code == 303


def test_unchecking_all_airports_of_a_country_is_rejected(client):
    token = login(client)
    response = client.post(
        "/searches", data=search_form(token, origins="CH", airport_countries="CH")
    )
    assert response.status_code == 422
    assert "Please select at least one airport for &#34;CH&#34;." in response.text


def test_invalid_form_shows_errors(client):
    token = login(client)
    response = client.post("/searches", data=search_form(token, origins="XXX", name=""))
    assert response.status_code == 422
    assert "Please enter a name." in response.text


def test_edit_pause_archive_and_delete(client):
    token = login(client)
    created = client.post("/searches", data=search_form(token), follow_redirects=False)
    url = created.headers["location"]

    edited = client.post(
        f"{url}/edit", data=search_form(token, destinations="BCN, MAD"), follow_redirects=True
    )
    assert "Revision 2" in edited.text

    assert "Resume" in client.post(f"{url}/pause", data={"csrf_token": token}).text
    archived = client.post(f"{url}/archive", data={"csrf_token": token})
    assert "Restore" in archived.text

    refused = client.post(f"{url}/delete", data={"csrf_token": token, "confirm_name": "falsch"})
    assert "exact name" in refused.text
    deleted = client.post(
        f"{url}/delete", data={"csrf_token": token, "confirm_name": "Zürich → Barcelona"}
    )
    assert "deleted permanently" in deleted.text
    assert client.get(url).status_code == 404


def test_english_by_default_and_german_on_request(client):
    token = login(client)
    assert '<html lang="en">' in client.get("/searches").text
    assert "Tracked searches" in client.get("/searches").text

    switched = client.get(
        "/language/de",
        headers={"referer": "http://testserver/searches/new"},
        follow_redirects=False,
    )
    assert switched.status_code == 303
    assert switched.headers["location"] == "/searches/new"
    page = client.get("/searches/new").text
    assert '<html lang="de">' in page
    assert "Neues Suchabo" in page

    response = client.post("/searches", data=search_form(token, name=""))
    assert "Bitte einen Namen angeben." in response.text
    assert client.get("/language/fr").status_code == 404


def test_language_switch_never_redirects_offsite(client):
    response = client.get(
        "/language/de", headers={"referer": "https://evil.example/phish"}, follow_redirects=False
    )
    assert response.headers["location"] == "/phish"
    for referer in ("https://evil.example//evil.example/x", "https://evil.example/\\evil.example"):
        response = client.get("/language/de", headers={"referer": referer}, follow_redirects=False)
        assert response.headers["location"] == "/"


def test_mock_provider_is_marked_as_invented(client, db):
    token = login(client)
    assert "fake-banner" in client.get("/searches").text
    url = client.post("/searches", data=search_form(token), follow_redirects=False).headers[
        "location"
    ]
    search_id = int(url.rsplit("/", 1)[1])
    revision_id = db.query(SearchRevision.id).filter_by(search_id=search_id).scalar()
    today = datetime.now(UTC)
    db.add(
        PriceHistory(
            search_id=search_id,
            search_revision_id=revision_id,
            source=PriceSource.LIVE,
            provider="mock",
            origin_iata="ZRH",
            destination_iata="BCN",
            departure_date=date(today.year + 1, 1, 10),
            cabin_class=CabinClass.ECONOMY,
            stops=0,
            price=Decimal("123.00"),
            currency="CHF",
            observed_at=today,
        )
    )
    db.commit()
    for stay in (7, 10):
        db.add(
            PriceHistory(
                search_id=search_id,
                search_revision_id=revision_id,
                source=PriceSource.LIVE,
                provider="mock",
                origin_iata="ZRH",
                destination_iata="BCN",
                departure_date=date(today.year + 1, 1, 10),
                return_date=date(today.year + 1, 1, 10 + stay),
                cabin_class=CabinClass.ECONOMY,
                stops=0,
                price=Decimal("150.00") + stay,
                currency="CHF",
                observed_at=today,
            )
        )
    db.commit()
    detail = client.get(url).text
    assert 'id="price-chart-data"' in detail and "data-price-filters" in detail
    # Two stay lengths (7 and 10 days) → the stay filter is offered.
    assert 'name="filter_stay"' in detail and 'data-stay="10"' in detail
    # Timestamps in Zurich time (UTC+1/+2), never raw UTC.
    local = today.astimezone(ZoneInfo("Europe/Zurich")).strftime("%Y-%m-%d %H:%M")
    assert local in detail
    assert 'data-origin="ZRH" data-destination="BCN" data-cabin="economy"' in detail
    assert 'class="fake-row"' in detail
    assert "fake-tag" in detail
    assert "invented prices" in detail


def test_real_provider_shows_no_fake_banner(db, session_factory):
    with make_client(session_factory, provider="google_flights") as real:
        login(real)
        page = real.get("/searches/new").text
        assert "fake-banner" not in page
        assert "not available with the data source" not in page


def test_children_note_when_provider_cannot_price_them(db, session_factory):
    with make_client(session_factory, provider="travelpayouts") as limited:
        login(limited)
        page = limited.get("/searches/new").text
        assert "not available with the data source &#34;travelpayouts&#34;" in page
        assert "are for one adult" in page


def test_suggestions(client, db):
    login(client)
    locations = client.get("/api/suggest/locations", params={"q": "es"}).json()
    assert locations[0] == {"value": "ES", "label": "Spain", "kind": "Country"}
    airports = client.get("/api/suggest/locations", params={"q": "zrh"}).json()
    assert airports[0]["value"] == "ZRH" and airports[0]["kind"] == "Airport"
    currencies = client.get("/api/suggest/currencies", params={"q": "fr"}).json()
    assert {"value": "CHF", "label": "Swiss franc", "kind": ""} in currencies

    client.get("/language/de")
    assert client.get("/api/suggest/locations", params={"q": "es"}).json()[0]["kind"] == "Land"


def test_suggestions_require_login(client):
    response = client.get("/api/suggest/locations", params={"q": "zrh"}, follow_redirects=False)
    assert response.status_code == 303


def test_airport_block_has_clear_button(client):
    token = login(client)
    review = client.post("/searches", data=search_form(token, origins="CH"))
    assert 'data-clear-choices="airports_CH"' in review.text
    assert 'data-suggest="/api/suggest/locations"' in review.text


def test_log_page_lists_queries_with_filters(client, db):
    token = login(client)
    url = client.post("/searches", data=search_form(token), follow_redirects=False).headers[
        "location"
    ]
    search_id = int(url.rsplit("/", 1)[1])
    base = {
        "job_id": None,
        "provider": "google_flights",
        "origin_iata": "ZRH",
        "destination_iata": "BCN",
        "departure_month": date(2026, 11, 1),
        "cabin_class": CabinClass.ECONOMY,
        "started_at": datetime.now(UTC),
        "duration_ms": 1500,
    }
    db.add_all(
        [
            QueryLog(
                **base,
                search_id=search_id,
                search_name="Zürich → Barcelona",
                outcome=QueryOutcome.OK,
                quotes_found=4,
                quotes_stored=4,
                results=[
                    {
                        "date": "2026-11-12",
                        "return": "2026-11-19",
                        "price": "134",
                        "currency": "CHF",
                        "stops": 0,
                    }
                ],
            ),
            QueryLog(
                **base,
                search_id=search_id,
                search_name="Zürich → Barcelona",
                outcome=QueryOutcome.FAILED,
                quotes_found=0,
                quotes_stored=0,
                error="Google Flights query failed (TypeError)",
            ),
            QueryLog(
                **base | {"provider": "mock"},
                search_id=999999,
                search_name="Gone",
                outcome=QueryOutcome.EMPTY,
                quotes_found=0,
                quotes_stored=0,
            ),
        ]
    )
    db.commit()

    page = client.get("/log").text
    assert "4 prices (4 new)" in page
    assert "Thu, 12 Nov 2026 → Thu, 19 Nov 2026: <strong>134.00 CHF</strong> · direct" in page
    assert "Google Flights query failed (TypeError)" in page
    assert "Gone" in page and "(deleted)" in page
    assert "fake-tag" in page  # the mock entry is marked as invented
    assert f'href="/searches/{search_id}"' in page

    failed_only = client.get("/log", params={"outcome": "failed"}).text
    assert "TypeError" in failed_only and "4 prices" not in failed_only
    one_search = client.get("/log", params={"search_id": str(search_id), "outcome": ""}).text
    assert "(deleted)" not in one_search
    assert client.get("/log", params={"search_id": "abc", "before": "x"}).status_code == 200
    for crafted in ("²", "9" * 30):
        assert (
            client.get("/log", params={"search_id": crafted, "before": crafted}).status_code == 200
        )


def _seed_price(db, search_id, *, details=None, price="123.00", observed_at=None):
    revision_id = db.query(SearchRevision.id).filter_by(search_id=search_id).scalar()
    today = datetime.now(UTC)
    row = PriceHistory(
        search_id=search_id,
        search_revision_id=revision_id,
        source=PriceSource.LIVE,
        provider="google_flights",
        origin_iata="ZRH",
        destination_iata="BCN",
        departure_date=date(today.year + 1, 1, 10),
        return_date=date(today.year + 1, 1, 17),
        cabin_class=CabinClass.ECONOMY,
        stops=1,
        price=Decimal(price),
        currency="CHF",
        observed_at=observed_at or today,
        details=details,
    )
    db.add(row)
    db.commit()
    return row


def test_flight_detail_page_with_segments_and_price_history(client, db):
    token = login(client)
    url = client.post("/searches", data=search_form(token), follow_redirects=False).headers[
        "location"
    ]
    search_id = int(url.rsplit("/", 1)[1])
    details = {
        "airlines": ["SWISS"],
        "segments": [
            {
                "flight": "LX 1952",
                "from": "ZRH",
                "to": "FRA",
                "arrival_date": "2027-01-10",
                "arrival_time": "08:30",
            },
            {
                "flight": "LH 1120",
                "from": "FRA",
                "to": "BCN",
                "departure_date": "2027-01-10",
                "departure_time": "09:35",
            },
        ],
        "url": "https://www.google.com/travel/flights?tfs=abc",
    }
    _seed_price(db, search_id, price="150.00", observed_at=datetime.now(UTC) - timedelta(hours=6))
    latest = _seed_price(db, search_id, details=details)

    detail = client.get(url).text
    assert f'href="/searches/{search_id}/flights/{latest.id}"' in detail
    assert 'class="route-dot" data-route="ZRH-BCN"' in detail

    page = client.get(f"/searches/{search_id}/flights/{latest.id}").text
    assert "https://www.flightradar24.com/data/flights/lx1952" in page
    assert "Layover in FRA: 1h 05m" in page
    assert "https://www.google.com/travel/flights?tfs=abc" in page
    assert "150.00 CHF" in page and "123.00 CHF" in page  # both polls of these dates
    assert client.get(f"/searches/{search_id}/flights/999999").status_code == 404


def test_poll_now_button(client, db):
    token = login(client)
    url = client.post("/searches", data=search_form(token), follow_redirects=False).headers[
        "location"
    ]
    assert "Poll now" in client.get(url).text
    # A fresh search already has its first poll due, so the worker is simply woken up …
    response = client.post(f"{url}/poll", data={"csrf_token": token})
    assert "Poll started" in response.text
    # … and a second click does not queue a parallel poll.
    assert "already running" in client.post(f"{url}/poll", data={"csrf_token": token}).text
    client.post(f"{url}/pause", data={"csrf_token": token})
    assert "Only active" in client.post(f"{url}/poll", data={"csrf_token": token}).text


def test_static_files_are_versioned(client):
    page = client.get("/login").text
    assert re.search(r'href="/static/css/app.css\?v=\d+"', page)
    assert re.search(r'src="/static/js/app.js\?v=\d+"', page)


def test_navigation_separates_center_links_from_right_utilities(client):
    login(client)
    page = client.get("/searches").text
    assert page.index('class="nav nav-primary"') < page.index('class="nav-utilities"')
    primary = page[page.index('class="nav nav-primary"') : page.index('class="nav-utilities"')]
    assert primary.index('href="/searches"') < primary.index('href="/searches/new"')
    assert primary.index('href="/searches/new"') < primary.index('href="/trips/new"')
    utilities = page[page.index('class="nav-utilities"') :]
    assert utilities.index('class="language-switch"') < utilities.index('href="/log"')
    assert utilities.index('href="/log"') < utilities.index('href="/settings"')
    assert utilities.index('href="/settings"') < utilities.index('action="/logout"')
    assert "Poll Log" in page
    assert "nav-dropdown" not in page


def test_platform_settings_override_switches_provider_without_restart(client):
    token = login(client)
    page = client.get("/settings").text
    assert "Platform Settings" in page
    response = client.post(
        "/settings/section/providers",
        data={"csrf_token": token, "flight_provider": "travelpayouts"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # The provider banner on every page now reflects the override, with no restart.
    assert "invents all prices" not in client.get("/searches").text
    assert 'value="travelpayouts" selected' in client.get("/settings").text


def test_global_currency_and_poll_interval_override_search_form_values(client, db):
    token = login(client)
    response = client.post(
        "/settings/section/searches",
        data={
            "csrf_token": token,
            "default_currency": "EUR",
            "default_poll_interval_minutes": "1440",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    form_page = client.get("/searches/new").text
    assert 'name="currency"' not in form_page
    assert 'name="poll_interval_hours"' not in form_page
    assert "Departure dates sampled per month" in form_page
    response = client.post(
        "/searches",
        data=search_form(token, currency="CHF", poll_interval_hours="1"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    search = db.query(Search).one()
    assert search.filters["currency"] == "EUR"
    assert search.poll_interval_minutes == 1440
    assert "every 24 h" in client.get(response.headers["location"]).text


def test_platform_settings_rejects_invalid_values(client):
    token = login(client)
    response = client.post(
        "/settings/section/worker",
        data={
            "csrf_token": token,
            "worker_tick_seconds": "0",
            "default_poll_interval_minutes": "360",
            "max_route_pairs_per_search": "50",
        },
    )
    assert "Could not save" in response.text


def test_import_reports_malformed_entries_instead_of_failing(client, db):
    from flighttracker.services.data_transfer import import_payload

    token = login(client)
    url = client.post("/searches", data=search_form(token), follow_redirects=False).headers[
        "location"
    ]
    entry = client.get(f"{url}/export").json()["searches"][0]
    db.query(Search).delete()
    db.commit()
    price = {
        "origin_iata": "ZRH",
        "destination_iata": "BCN",
        "cabin_class": "economy",
        "departure_date": "2026-12-01",
        "return_date": None,
        "stops": 0,
        "provider": "mock",
        "source": "live",
        "observed_at": "2026-10-01T08:00:00+00:00",
        "currency": "CHF",
    }
    broken = [
        "not an object",
        entry | {"name": "Bad filters", "filters": {"x": 1}},
        entry | {"name": "Bad price", "price_history": [price | {"price": "abc"}]},
        entry | {"name": "NaN price", "price_history": [price | {"price": "NaN"}]},
        entry
        | {"name": "Unknown airport", "locations": [{"role": "origin", "airport_code": "QQQ"}]},
    ]
    result = import_payload(db, {"format": 2, "searches": broken, "trips": []})
    assert len(result.errors) == len(broken)
    db.rollback()

    files = {"file": ("x.json", json.dumps({"format": 2, "searches": broken}), "application/json")}
    response = client.post(
        "/settings/import", data={"csrf_token": token}, files=files, follow_redirects=False
    )
    assert response.status_code == 303
    assert "Import failed" in client.get("/settings").text
    assert client.get("/searches").status_code == 200

    # A valid active Suchabo is due right away, like a newly created one.
    assert not import_payload(db, {"format": 2, "searches": [entry]}).errors
    assert db.query(Search).one().next_poll_at is not None


def test_platform_settings_export_and_import_round_trip(client, db):
    token = login(client)
    url = client.post("/searches", data=search_form(token), follow_redirects=False).headers[
        "location"
    ]
    search_id = int(url.rsplit("/", 1)[-1])

    export = client.get(f"{url}/export")
    assert export.headers["content-type"] == "application/json"
    payload = export.json()
    assert payload["searches"][0]["name"] == "Zürich → Barcelona"

    all_data = client.get("/settings/export").json()
    assert any(s["name"] == "Zürich → Barcelona" for s in all_data["searches"])

    # Importing a search's own export only extends it – it must not create a duplicate.
    files = {"file": ("export.json", json.dumps(payload), "application/json")}
    import_response = client.post(
        "/settings/import", data={"csrf_token": token}, files=files, follow_redirects=False
    )
    assert import_response.status_code == 303
    flash = client.get("/settings").text
    assert "1 tracked searches and 0 Trips already existed" in flash
    assert client.get(f"/searches/{search_id}").status_code == 200

    from datetime import date, timedelta

    starts = date.today() + timedelta(days=14)
    trip_response = client.post(
        "/trips",
        data={
            "csrf_token": token,
            "name": "Export trip",
            "starts_on": starts.isoformat(),
            "ends_on": (starts + timedelta(days=7)).isoformat(),
            "adults": "1",
            "children": "2",
            "cabin_classes": ["economy"],
            "max_stops": "any",
            "leg_origin": ["ZRH", "BCN"],
            "leg_destination": ["BCN", "MAD"],
            "min_layover_days": ["", "1"],
            "max_layover_days": ["", "3"],
        },
        follow_redirects=False,
    )
    assert trip_response.status_code == 303
    all_data = client.get("/settings/export").json()
    assert all_data["format"] == 2
    trip_data = next(trip for trip in all_data["trips"] if trip["name"] == "Export trip")
    assert [leg["position"] for leg in trip_data["legs"]] == [0, 1]
    assert trip_data["legs"][1]["min_layover_days"] == 1

    db.query(Trip).delete()
    db.query(Search).delete()
    db.commit()
    files = {"file": ("all-data.json", json.dumps(all_data), "application/json")}
    imported = client.post(
        "/settings/import", data={"csrf_token": token}, files=files, follow_redirects=False
    )
    assert imported.status_code == 303
    db.expire_all()
    restored = db.query(Trip).one()
    assert restored.name == "Export trip"
    assert [leg.position for leg in restored.legs] == [0, 1]
    assert restored.legs[1].min_layover_days == 1

    repeated = client.post(
        "/settings/import", data={"csrf_token": token}, files=files, follow_redirects=False
    )
    assert repeated.status_code == 303
    db.expire_all()
    assert db.query(Trip).count() == 1

    legacy = {key: value for key, value in all_data.items() if key != "trips"}
    legacy["format"] = 1
    legacy_file = {"file": ("legacy.json", json.dumps(legacy), "application/json")}
    legacy_response = client.post(
        "/settings/import",
        data={"csrf_token": token},
        files=legacy_file,
        follow_redirects=False,
    )
    assert legacy_response.status_code == 303
    assert db.query(Trip).count() == 1


def test_trip_planner_creates_and_groups_independent_legs(client):
    from datetime import date, timedelta

    token = login(client)
    starts = date.today() + timedelta(days=14)
    response = client.post(
        "/trips",
        data={
            "csrf_token": token,
            "name": "Catalonia loop",
            "starts_on": starts.isoformat(),
            "ends_on": (starts + timedelta(days=7)).isoformat(),
            "poll_interval_hours": "6",
            "adults": "1",
            "currency": "CHF",
            "cabin_classes": ["economy"],
            "max_stops": "1",
            "leg_origin": ["ZRH", "BCN"],
            "leg_destination": ["BCN", "MAD"],
            "min_layover_days": ["", "1"],
            "max_layover_days": ["", "2"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    trip_url = response.headers["location"]
    trip_page = client.get(trip_url).text
    assert "Catalonia loop" in trip_page
    assert "ZRH" in trip_page and "MAD" in trip_page
    assert "No complete options yet" in trip_page

    searches_page = client.get("/searches").text
    assert "Catalonia loop" in searches_page
    assert "ZRH–BCN" in searches_page
    assert "BCN–MAD" in searches_page
    leg_search_id = int(re.search(r'href="/searches/(\d+)"', trip_page).group(1))
    from flighttracker.services.trips import get_trip_for_search

    with client.app.state.session_factory() as session:
        assert get_trip_for_search(session, leg_search_id) is not None
    leg_page = client.get(f"/searches/{leg_search_id}").text
    assert f'href="{trip_url}"' in leg_page
    assert "Poll trip now" in leg_page
    assert "Pause" not in leg_page and "Edit" not in leg_page
    poll_response = client.post(
        f"/searches/{leg_search_id}/poll",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert poll_response.headers["location"] == trip_url


def test_trip_planner_reviews_country_airports_and_saves_selected_routes(client):
    from datetime import date, timedelta

    token = login(client)
    form = {
        "csrf_token": token,
        "name": "Country hop",
        "starts_on": (date.today() + timedelta(days=14)).isoformat(),
        "ends_on": (date.today() + timedelta(days=21)).isoformat(),
        "poll_interval_hours": "24",
        "adults": "2",
        "children": "3",
        "currency": "CHF",
        "cabin_classes": ["economy"],
        "max_stops": "any",
        "leg_origin": ["CH", "ES"],
        "leg_destination": ["ES", "CH"],
        "min_layover_days": ["", "1"],
        "max_layover_days": ["", "2"],
    }
    review = client.post("/trips", data=form)
    assert review.status_code == 200
    assert 'data-suggest="/api/suggest/locations"' in review.text
    assert "The largest airports of each country are preselected" in review.text
    assert 'name="trip_airports_0_CH" value="GVA" checked' in review.text
    assert 'name="trip_airports_0_ES" value="BCN" checked' in review.text
    assert 'name="children"' in review.text

    form.update(
        {
            "trip_airports_reviewed": ["0:CH", "0:ES", "1:CH", "1:ES"],
            "trip_airports_0_CH": ["ZRH", "GVA"],
            "trip_airports_0_ES": ["BCN", "MAD"],
            "trip_airports_1_CH": ["ZRH", "GVA"],
            "trip_airports_1_ES": ["BCN", "MAD"],
        }
    )
    response = client.post("/trips", data=form, follow_redirects=False)
    assert response.status_code == 303
    trip_url = response.headers["location"]
    trip_id = int(trip_url.rsplit("/", 1)[-1])

    from flighttracker.services.searches import spec_of
    from flighttracker.services.trips import get_trip

    with client.app.state.session_factory() as session:
        trip = get_trip(session, trip_id)
        assert spec_of(trip.legs[0].search).country_airports == {
            "CH": {"GVA", "ZRH"},
            "ES": {"BCN", "MAD"},
        }
        assert spec_of(trip.legs[1].search).country_airports == {
            "CH": {"GVA", "ZRH"},
            "ES": {"BCN", "MAD"},
        }
        assert trip.legs[0].search.filters["adults"] == 2
        assert trip.legs[0].search.filters["children"] == 3


def test_detail_shows_price_trends_per_flight_and_in_chart(client, db):
    token = login(client)
    url = client.post("/searches", data=search_form(token), follow_redirects=False).headers[
        "location"
    ]
    search_id = int(url.rsplit("/", 1)[1])
    revision_id = db.query(SearchRevision.id).filter_by(search_id=search_id).scalar()
    now = datetime.now(UTC)
    departure = date.today() + timedelta(days=60)

    def observe(price: str, days_ago: int, destination: str = "BCN"):
        db.add(
            PriceHistory(
                search_id=search_id,
                search_revision_id=revision_id,
                source=PriceSource.LIVE,
                provider="mock",
                origin_iata="ZRH",
                destination_iata=destination,
                departure_date=departure,
                cabin_class=CabinClass.ECONOMY,
                stops=0,
                price=Decimal(price),
                currency="CHF",
                observed_at=now - timedelta(days=days_ago),
            )
        )

    observe("200.00", 30)
    observe("180.00", 7)
    observe("170.00", 1)
    observe("150.00", 0)
    observe("100.00", 1, "MAD")
    db.commit()

    page = client.get(url).text
    assert 'class="spark spark-down"' in page
    assert "data-chart-trends checked" in page
    assert '"first": 200.0' in page
    assert '"hist": [200.0, 180.0, 170.0, 150.0]' in page
    assert '"period": "1d", "amount": -20.0' in page
    assert '"period": "1w", "amount": -30.0' in page
    assert '"period": "1m", "amount": -50.0' in page
    assert "1d ▼ 20.00 CHF" in page
    assert "1w ▼ 30.00 CHF" in page
    assert "1m ▼ 50.00 CHF" in page
    assert "200.00 CHF" in page and "150.00 CHF" in page
