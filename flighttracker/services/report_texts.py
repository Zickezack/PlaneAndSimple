"""Poll reports in words: rule-based, translated lines – the same on the page, in the list and
in mails. The AI's summary comes on top, and only where it is in the reader's language.

Every text is a literal `translate("…")` call, so the catalog check (tests/unit/test_i18n.py)
sees it.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from flighttracker.domain.deals import Deal
from flighttracker.domain.poll_report import Flight, SearchReport, from_facts, notable
from flighttracker.i18n import format_weekday_date, translate
from flighttracker.models import PollReport
from flighttracker.services.mail_messages import format_amount


def flight_text(flight: Flight, locale: str) -> str:
    dates = format_weekday_date(flight.departure, locale)
    if flight.returning:
        dates += f" – {format_weekday_date(flight.returning, locale)}"
    cabin = "" if flight.cabin == "economy" else f", {flight.cabin.replace('_', ' ')}"
    return f"{flight.origin} → {flight.destination}, {dates}{cabin}"


def delta(amount: Decimal, currency: str) -> str:
    if amount < 0:
        return f"▼ {format_amount(-amount, currency)}"
    if amount > 0:
        return f"▲ {format_amount(amount, currency)}"
    return "±0"


def _cheapest_line(report: SearchReport, cheapest: Flight, locale: str) -> str:
    price = format_amount(cheapest.price, report.currency)
    flight = flight_text(cheapest, locale)
    change = report.cheapest_change
    if change is None:
        return translate(
            "Cheapest flight now {price}: {flight}.", locale, price=price, flight=flight
        )
    return translate(
        "Cheapest flight now {price} ({change} since the poll before): {flight}.",
        locale,
        price=price,
        change=delta(change, report.currency),
        flight=flight,
    )


def _counts_line(report: SearchReport, locale: str) -> str:
    if not report.compared:
        if report.flights == 1:
            return translate("First poll: 1 flight found.", locale)
        return translate("First poll: {n} flights found.", locale, n=report.flights)
    return translate(
        "Since the poll before: {cheaper} cheaper, {dearer} dearer, {same} about the same (a "
        "change counts from 3 %).",
        locale,
        cheaper=report.cheaper,
        dearer=report.dearer,
        same=report.compared - report.cheaper - report.dearer,
    )


def _change_params(flight: Flight, currency: str, locale: str) -> dict:
    return {
        "flight": flight_text(flight, locale),
        "change": delta(flight.change or Decimal(0), currency),
        "price": format_amount(flight.price, currency),
    }


def search_lines(report: SearchReport, locale: str) -> list[str]:
    c = report.currency
    cheapest = report.cheapest
    if cheapest is None:
        return [translate("No prices in this poll.", locale)]
    lines = []
    if report.compared and not notable(report):
        lines.append(translate("No notable change since the poll before.", locale))
    lines += [_cheapest_line(report, cheapest, locale), _counts_line(report, locale)]
    if report.new_lows:
        best = {"flight": flight_text(report.new_lows[0], locale)}
        best["price"] = format_amount(report.new_lows[0].price, c)
        if report.new_low_count == 1:
            lines.append(translate("A new lowest price: {flight} for {price}.", locale, **best))
        else:
            lines.append(
                translate(
                    "{n} new lowest prices, e.g. {flight} for {price}.",
                    locale,
                    n=report.new_low_count,
                    **best,
                )
            )
    if report.drops and report.drops[0].key != cheapest.key:
        params = _change_params(report.drops[0], c, locale)
        lines.append(translate("Biggest drop: {flight}, {change} to {price}.", locale, **params))
    if report.rises:
        params = _change_params(report.rises[0], c, locale)
        lines.append(translate("Biggest rise: {flight}, {change} to {price}.", locale, **params))
    for deal in report.deals[:3]:
        lines.append(
            translate(
                "Unusually cheap: {flight} for {price} – {percent} % below the usual {usual} of "
                "this route.",
                locale,
                flight=flight_text(deal.flight, locale),
                price=format_amount(deal.flight.price, c),
                percent=deal.percent_below,
                usual=format_amount(deal.usual, c),
            )
        )
    return lines


def search_short(report: SearchReport, locale: str) -> str:
    """One line for the list: cheapest price and its change, new lowest and unusually cheap
    prices."""
    if report.cheapest is None:
        return ""
    parts = [format_amount(report.cheapest.price, report.currency)]
    if report.cheapest_change:
        parts[0] += f" {delta(report.cheapest_change, report.currency)}"
    if report.new_low_count == 1:
        parts.append(translate("1 new lowest price", locale))
    elif report.new_low_count:
        parts.append(translate("{n} new lowest prices", locale, n=report.new_low_count))
    if len(report.deals) == 1:
        parts.append(translate("1 unusually cheap price", locale))
    elif report.deals:
        parts.append(translate("{n} unusually cheap prices", locale, n=len(report.deals)))
    return " · ".join(parts)


def short_lines(rows: dict[int, PollReport], locale: str) -> dict[int, str]:
    """`search_short` per id of `rows` (from `poll_reports.latest_reports`)."""
    lines = {}
    for key, row in rows.items():
        report = from_facts(row.facts)
        if report is not None and (line := search_short(report, locale)):
            lines[key] = line
    return lines


@dataclass(frozen=True)
class ReportView:
    created_at: datetime
    report: SearchReport
    lines: list[str]
    summary: str | None  # the AI's words – only when in the reader's language
    summary_locale: str | None


def view(row: PollReport | None, locale: str) -> ReportView | None:
    report = from_facts(row.facts) if row else None
    if row is None or report is None:
        return None
    summary = row.summary if row.summary_locale == locale else None
    return ReportView(
        row.created_at, report, search_lines(report, locale), summary, row.summary_locale
    )


def deals_by_flight(view: "ReportView | None") -> dict[tuple, Deal]:
    """The unusually cheap prices of a report by flight and price (for the flights table)."""
    if view is None:
        return {}
    return {(*deal.flight.key, deal.flight.price): deal for deal in view.report.deals}


def summary_block(row: PollReport | None, locale: str) -> str | None:
    """The AI's words about the poll as a mail paragraph – only in their own language."""
    if row is None or not row.summary or row.summary_locale != locale:
        return None
    title = translate("Since the poll before:", locale)
    note = translate("Written by AI from the numbers of the app.", locale)
    return f"{title}\n{row.summary}\n({note})"


def mail_blocks(row: PollReport | None, locale: str) -> list[str]:
    """Paragraphs for a mail after the same poll – only when it was notable: the AI's words, or
    else the first lines of the report."""
    report = from_facts(row.facts) if row else None
    if row is None or report is None or not row.notable:
        return []
    if summary := summary_block(row, locale):
        return [summary]
    lines = "\n".join(f"• {line}" for line in search_lines(report, locale)[:4])
    return [translate("Since the poll before:", locale), lines]
