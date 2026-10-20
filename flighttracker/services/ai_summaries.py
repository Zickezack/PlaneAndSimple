"""What the worker asks the AI by itself: a few sentences about a notable poll of a tracked
search or a Trip, and the overview of a price digest. Every fact comes from the app (the poll
reports, domain/poll_report.py and domain/trip_report.py); the AI only words them – with the
guardrails of every request (services/ai_prompts.system_prompt).
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.deals import DEAL_SHARE
from flighttracker.domain.poll_report import CHANGE_SHARE, Flight, SearchReport
from flighttracker.domain.trip_report import TripReport
from flighttracker.models import AIUsage
from flighttracker.services import ai, ai_prompts
from flighttracker.services.mail_messages import format_amount

# Requests the worker sends by itself (features in the usage log).
BACKGROUND_FEATURES = ("summary", "trip_summary", "digest")
# After a failed one it waits this long: a server that does not answer must not slow every poll.
PAUSE = timedelta(minutes=15)
SHARE = f"{int(CHANGE_SHARE * 100)} %"

SEARCH_TASK = """\
Below are the facts of the latest poll of a tracked search, compared with the poll before.
Write at most three short sentences for its owner: the most important change first, then what
it means for booking now – without predicting prices. No lists, no headings.

{facts}"""


TRIP_TASK = """\
Below are the facts of the latest poll of a planned Trip – several one-way flights in a row,
each booked separately – compared with the poll before. Write at most three short sentences for
its owner: the most important change first, then what it means for booking now – without
predicting prices. No lists, no headings.

{facts}"""


DIGEST_TASK = """\
Below are the changes of the tracked searches and Trips of one user since {since}. Write three
to five short sentences for this user: the most important developments first, naming the
tracked search or Trip, then what they mean for booking now – without predicting prices. No
lists, no headings.

{facts}"""


def paused(session: Session, now: datetime) -> bool:
    """A request of the worker failed less than PAUSE ago."""
    failed = session.scalar(
        select(func.max(AIUsage.requested_at)).where(
            AIUsage.feature.in_(BACKGROUND_FEATURES), AIUsage.ok.is_(False)
        )
    )
    return failed is not None and failed > now - PAUSE


def flight_line(flight: Flight, currency: str) -> str:
    dates = flight.departure.isoformat()
    if flight.returning:
        dates += f" to {flight.returning.isoformat()}"
    cabin = flight.cabin.replace("_", " ")
    price = format_amount(flight.price, currency)
    return f"{flight.origin}→{flight.destination} {dates} ({cabin}) at {price}"


def search_facts(
    name: str, route: str, report: SearchReport, before: str = "the poll before"
) -> str:
    """The report as plain lines for the prompt – only what the app computed. `before`: what it
    is compared with (a digest compares with the start of its period)."""
    c = report.currency

    def money(amount):
        return format_amount(amount, c)

    lines = [
        f"Tracked search: {name} ({route}); prices in {c}.",
        f"Flights in the latest poll: {report.flights}; with a price at {before}: "
        f"{report.compared}; of these {report.cheaper} cheaper and {report.dearer} dearer by "
        f"{SHARE} or more.",
    ]
    if report.cheapest:
        line = f"Cheapest flight now: {flight_line(report.cheapest, c)}"
        if report.cheapest_before is not None:
            line += f"; at {before} the cheapest was {money(report.cheapest_before)}"
        lines.append(line + ".")
    if report.months:
        months = ", ".join(
            f"{m.month:%Y-%m} {money(m.price)}"
            + (f" ({money(m.previous)})" if m.previous is not None else "")
            for m in report.months
        )
        lines.append(f"Cheapest per departure month now (before): {months}.")
    if report.new_low_count:
        examples = "; ".join(
            f"{flight_line(f, c)}, lowest before {money(f.lowest_before)}" for f in report.new_lows
        )
        lines.append(
            f"New lowest prices ({SHARE} or more below the flight's lowest so far): "
            f"{report.new_low_count}, for example {examples}."
        )
    for title, flights in (("drops", report.drops), ("rises", report.rises)):
        if flights:
            changes = "; ".join(f"{flight_line(f, c)}, before {money(f.previous)}" for f in flights)
            lines.append(f"Biggest {title} since {before}: {changes}.")
    if report.deals:
        deals = "; ".join(
            f"{flight_line(d.flight, c)}, usual {money(d.usual)}" for d in report.deals
        )
        share = f"{int(DEAL_SHARE * 100)} %"
        lines.append(
            f"Unusually cheap (at most {share} of the usual price of the route in this poll; "
            f"possibly an error fare): {deals}."
        )
    return "\n".join(lines)


def summarize_search(
    settings: Settings,
    name: str,
    route: str,
    report: SearchReport,
    locale: str,
    usage: ai.UsageLog | None = None,
) -> str:
    task = SEARCH_TASK.format(facts=search_facts(name, route, report))
    return ai.complete(settings, ai_prompts.system_prompt(settings, locale), task, usage=usage).text


def trip_facts(name: str, report: TripReport, before: str = "the poll before") -> str:
    c = report.currency

    def money(amount):
        return format_amount(amount, c)

    lines = [f"Trip: {name}; prices in {c}, the sum of separate one-way tickets."]
    before = f"; at {before}: {report.options_before}" if report.options_before is not None else ""
    lines.append(f"Working options now: {report.options}{before}.")
    if report.total is None:
        lines.append("No working option was found in this poll.")
    else:
        line = f"Cheapest option now: {money(report.total)}"
        if report.total_before is not None:
            line += f"; at {before}: {money(report.total_before)}"
        if report.lowest_before is not None:
            line += f"; lowest before this poll: {money(report.lowest_before)}"
        lines.append(line + ".")
        if report.options_before is not None and not report.same_option:
            lines.append(f"It has other flights than the cheapest option at {before}.")
        legs = "; ".join(
            f"{i}. {leg.origin}→{leg.destination} {leg.departure.isoformat()}"
            + (f" {leg.departure_time}" if leg.departure_time else "")
            + f" at {money(leg.price)}"
            + (f" (before {money(leg.previous)})" if leg.previous is not None else "")
            for i, leg in enumerate(report.legs, 1)
        )
        lines.append(f"Its legs: {legs}.")
    return "\n".join(lines)


def summarize_trip(
    settings: Settings,
    name: str,
    report: TripReport,
    locale: str,
    usage: ai.UsageLog | None = None,
) -> str:
    task = TRIP_TASK.format(facts=trip_facts(name, report))
    return ai.complete(settings, ai_prompts.system_prompt(settings, locale), task, usage=usage).text


def summarize_digest(
    settings: Settings,
    since: str,
    facts: list[str],
    locale: str,
    usage: ai.UsageLog | None = None,
) -> str:
    """`facts`: one block per tracked search or Trip (`search_facts`, `trip_facts`)."""
    task = DIGEST_TASK.format(since=since, facts="\n\n".join(facts))
    return ai.complete(settings, ai_prompts.system_prompt(settings, locale), task, usage=usage).text
