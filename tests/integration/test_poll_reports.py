"""After a poll: the report of what changed, the AI's summary of a notable one, the block on the
tracked search's page, the line in the list and the paragraph in price alert mails."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from flighttracker.domain import trip_report
from flighttracker.domain.filters import CabinClass
from flighttracker.models import (
    AIUsage,
    FetchJob,
    JobKind,
    JobStatus,
    PollReport,
    PriceAlert,
    PriceHistory,
    PriceSource,
    SearchRevision,
    Share,
)
from flighttracker.providers.mock import MockProvider
from flighttracker.services import ai, mail
from flighttracker.services.jobs import request_trip_poll
from flighttracker.services.trips import create_trip
from flighttracker.web.app import create_app
from flighttracker.worker.after_poll import after_search_poll
from flighttracker.worker.loop import process_next_job
from tests.integration.test_ai_pages import ai_settings
from tests.integration.test_services import create
from tests.integration.test_trips import trip_input
from tests.integration.test_users import add_user, login_as
from tests.integration.test_web import login

YEAR = datetime.now(UTC).year + 1
SUMMARY = "Prices fell: the cheapest flight now costs 150.00 CHF instead of 200.00 CHF."


def poll(db, search, prices: dict[int, str], at: datetime) -> FetchJob:
    """A completed poll at `at` that found these round trips (day of January → price)."""
    revision = db.scalar(select(SearchRevision.id).where(SearchRevision.search_id == search.id))
    job = FetchJob(
        search_id=search.id,
        kind=JobKind.POLL,
        status=JobStatus.DONE,
        run_after=at,
        created_at=at,
        started_at=at,
        finished_at=at + timedelta(minutes=5),
    )
    db.add(job)
    for day, price in prices.items():
        db.add(
            PriceHistory(
                search_id=search.id,
                search_revision_id=revision,
                source=PriceSource.LIVE,
                provider="google_flights",
                origin_iata="ZRH",
                destination_iata="BCN",
                departure_date=date(YEAR, 1, day),
                return_date=date(YEAR, 1, day + 7),
                cabin_class=CabinClass.ECONOMY,
                stops=0,
                price=Decimal(price),
                currency="CHF",
                adults=1,
                children=0,
                observed_at=at + timedelta(minutes=1),
            )
        )
    db.commit()
    return job


def polls(db, search, settings, *rounds: dict[int, str]) -> list[PollReport]:
    """Polls six hours apart, each followed by what the worker does after a poll."""
    start = datetime.now(UTC) - timedelta(days=1)
    reports = []
    for number, prices in enumerate(rounds):
        job = poll(db, search, prices, start + timedelta(hours=6 * number))
        after_search_poll(db, settings, job.id, start + timedelta(hours=6 * number, minutes=6))
        reports.append(db.scalar(select(PollReport).where(PollReport.job_id == job.id)))
    return reports


@pytest.fixture
def ollama(monkeypatch):
    """Ollama answering every chat request with `text`; returns the prompts it got."""

    def install(text=SUMMARY):
        prompts = []

        def post(url, json=None, headers=None, timeout=None):
            prompts.append(json["messages"][-1]["content"])
            return httpx.Response(200, json={"message": {"content": text}})

        monkeypatch.setattr(ai.httpx, "post", post)
        return prompts

    return install


def test_only_a_notable_poll_is_worded_by_the_ai(db, ollama):
    search = create(db)
    db.commit()
    prompts = ollama()
    first, notable, quiet = polls(
        db,
        search,
        ai_settings(),
        {3: "200", 4: "200", 5: "210"},
        {3: "150", 4: "200", 5: "210"},  # the cheapest fell by a quarter
        {3: "150", 4: "201", 5: "210"},
    )
    assert not first.notable and first.summary is None  # nothing to compare yet
    assert notable.notable and (notable.summary, notable.summary_locale) == (SUMMARY, "en")
    assert not quiet.notable and quiet.summary is None
    assert len(prompts) == 1  # only the notable poll was sent
    assert "Cheapest flight now: ZRH→BCN" in prompts[0] and "150.00 CHF" in prompts[0]
    assert "at the poll before the cheapest was 200.00 CHF" in prompts[0]
    assert notable.facts["cheapest_before"] == "200.00"
    usage = db.scalars(select(AIUsage)).all()
    assert [(u.feature, u.ok) for u in usage] == [("summary", True)]

    off = ai_settings(ai_poll_summaries=False)
    (_, switched_off) = polls(db, create(db), off, {3: "200"}, {3: "100"})
    assert switched_off.notable and switched_off.summary is None  # facts only


def test_after_a_failed_request_the_worker_pauses(db, monkeypatch):
    calls = []

    def unreachable(*args, **kwargs):
        calls.append(1)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ai.httpx, "post", unreachable)
    search = create(db)
    db.commit()
    reports = polls(db, search, ai_settings(), {3: "200"}, {3: "150"})
    assert reports[1].notable and reports[1].summary is None and len(calls) == 1
    # Another notable poll right after: no request while the pause lasts.
    job = poll(db, search, {3: "100"}, datetime.now(UTC) - timedelta(minutes=10))
    after_search_poll(db, ai_settings(), job.id, datetime.now(UTC))
    assert len(calls) == 1
    assert db.scalar(select(PollReport).where(PollReport.job_id == job.id)).notable


def test_page_list_and_alert_mail_tell_what_changed(db, session_factory, ollama, monkeypatch):
    sent: list[mail.Mail] = []
    monkeypatch.setattr(mail, "send", lambda settings, message: sent.append(message))
    settings = ai_settings(
        smtp_host="smtp.example.test",
        mail_from="app@example.test",
        public_base_url="https://flights.example.test",
    )
    alice = add_user(db, "alice", email="alice@example.test", locale="en")
    search = create(db)
    search.owner_id = alice.id
    db.add(
        PriceAlert(search_id=search.id, user_id=alice.id, max_price=Decimal("180"), currency="CHF")
    )
    db.commit()
    ollama()
    polls(db, search, settings, {3: "200", 4: "200"}, {3: "150", 4: "200"})
    (alert_mail,) = sent
    assert "Since the poll before:" in alert_mail.body and SUMMARY in alert_mail.body
    assert "Written by AI from the numbers of the app." in alert_mail.body

    with TestClient(create_app(settings, session_factory)) as client:
        login(client)
        page = client.get(f"/searches/{search.id}").text
        assert "Since the poll before" in page and SUMMARY in page
        assert 'lang="en"' in page and "Written by AI from the numbers below." in page
        assert "Cheapest flight now 150.00 CHF (▼ 50.00 CHF since the poll before)" in page
        assert "150.00 CHF ▼ 50.00 CHF · 1 new lowest price" in client.get("/searches").text
        # In German: the facts in German, the English summary left out.
        client.get("/language/de")
        page = client.get(f"/searches/{search.id}").text
        assert SUMMARY not in page and "Günstigster Flug jetzt 150.00 CHF" in page

        # Clearing the price history also clears what earlier polls changed.
        token = login(client)
        client.post(
            f"/searches/{search.id}/clear-history",
            data={"csrf_token": token, "confirm_clear": search.name},
        )
        db.expire_all()
        assert db.scalars(select(PollReport).where(PollReport.search_id == search.id)).all() == []


USUAL = {day: "200" for day in range(1, 10)}  # nine departure dates at the usual price


def test_unusually_cheap_prices_are_mailed_once_and_marked(db, session_factory, monkeypatch):
    sent: list[mail.Mail] = []
    monkeypatch.setattr(mail, "send", lambda settings, message: sent.append(message))
    settings = ai_settings(
        ai_provider="",  # facts only
        smtp_host="smtp.example.test",
        mail_from="app@example.test",
        public_base_url="https://flights.example.test",
    )
    owner = add_user(db, "owner", email="owner@example.test")
    friend = add_user(db, "friend", email="friend@example.test", locale="de")
    quiet = add_user(db, "quiet", email="quiet@example.test", deal_mails=False)
    search = create(db)
    search.owner_id = owner.id
    db.add_all(
        [Share(search_id=search.id, user_id=user.id, can_edit=False) for user in (friend, quiet)]
    )
    db.commit()
    polls(db, search, settings, {**USUAL, 20: "90"}, {**USUAL, 20: "90"}, {**USUAL, 20: "80"})
    # First poll: owner and friend; second: already mailed; third: cheaper again.
    assert [m.to for m in sent] == [
        "owner@example.test",
        "friend@example.test",
        "owner@example.test",
        "friend@example.test",
    ]
    assert sent[0].kind == "deal" and sent[0].subject == "Unusually cheap: Test from 90.00 CHF"
    assert "90.00 CHF (usually 200.00 CHF, 55 % less)" in sent[0].body
    assert "https://flights.example.test/settings#personal" in sent[0].body
    assert "Ungewöhnlich günstig" in sent[1].subject  # in the friend's language
    assert "80.00 CHF" in sent[2].body

    with TestClient(create_app(settings, session_factory)) as client:
        login(client)
        page = client.get(f"/searches/{search.id}").text
        assert page.count(">Unusually cheap</span>") == 1  # the flight in the table
        assert "60 % below the usual 200.00 CHF of this route" in page

        # The switch in the personal settings.
        token = login_as(client, "owner")
        client.post(
            "/settings/personal",
            data={
                "csrf_token": token,
                "locale": "",
                "timezone": "",
                "email": "owner@example.test",
                "deal_mails": "false",
            },
        )
        db.refresh(owner)
        assert owner.deal_mails is False


class RealPrices(MockProvider):
    """The mock's prices as if they were real (fake data is never reported), times `factor`."""

    name = "google_flights"
    fake_data = False

    def __init__(self, clock, factor="1"):
        super().__init__(clock)
        self.factor = Decimal(factor)

    def fetch_current(self, query):
        return [
            replace(quote, price=(quote.price * self.factor).quantize(Decimal("0.01")))
            for quote in super().fetch_current(query)
        ]


def test_a_trip_poll_is_reported_and_summarised(db, session_factory, ollama):
    prompts = ollama("The cheapest option got cheaper.")
    trip = create_trip(db, trip_input(), max_route_pairs=50)
    db.commit()
    settings = ai_settings()
    start = datetime.now(UTC).replace(hour=1, minute=0)  # both polls on the same day
    for at, factor in ((start, "1"), (start + timedelta(hours=6), "0.8")):
        assert request_trip_poll(db, trip, at)
        db.commit()
        assert process_next_job(
            session_factory,
            RealPrices(lambda at=at: at, factor),
            clock=lambda at=at: at,
            base_settings=settings,
        )
    first, second = db.scalars(
        select(PollReport).where(PollReport.trip_id == trip.id).order_by(PollReport.id)
    ).all()
    assert not first.notable and second.notable
    report = trip_report.from_facts(second.facts)
    assert report.total < report.total_before and report.same_option
    assert second.summary == "The cheapest option got cheaper."
    assert len(prompts) == 1 and "Trip: Island loop" in prompts[0]
    assert "Its legs: 1. ZRH→BCN" in prompts[0]

    with TestClient(create_app(settings, session_factory)) as client:
        login(client)
        page = client.get(f"/trips/{trip.id}").text
        assert "The cheapest option got cheaper." in page and "Cheapest option now" in page
        listing = client.get("/searches").text
        assert f"{trip_report.from_facts(second.facts).options} working options" in listing
        token = login(client)
        client.post(
            f"/trips/{trip.id}/clear-history",
            data={"csrf_token": token, "confirm_clear": trip.name},
        )
    db.expire_all()
    assert db.scalars(select(PollReport).where(PollReport.trip_id == trip.id)).all() == []
