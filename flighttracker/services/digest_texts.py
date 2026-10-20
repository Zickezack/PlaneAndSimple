"""The price digest in words: what changed per tracked search and Trip since the start of the
period, as translated lines, and the mail around it (services/digest.py builds the facts).

Every text is a literal `translate("…")` call, so the catalog check sees it.
"""

from flighttracker.domain.poll_report import SearchReport
from flighttracker.domain.trip_report import TripReport
from flighttracker.i18n import translate
from flighttracker.models import DigestFrequency, User
from flighttracker.services.mail import Mail
from flighttracker.services.mail_messages import body, format_amount, locale_of
from flighttracker.services.report_texts import change_params, delta, flight_text


def period_lines(report: SearchReport, since: str, locale: str) -> list[str]:
    """A tracked search's changes since `since` (a date, already formatted) for the digest."""
    c = report.currency
    cheapest = report.cheapest
    if cheapest is None:
        return [translate("No prices in this poll.", locale)]
    price, flight = format_amount(cheapest.price, c), flight_text(cheapest, locale)
    if report.cheapest_change is None:
        lines = [
            translate("Cheapest flight now {price}: {flight}.", locale, price=price, flight=flight)
        ]
    else:
        lines = [
            translate(
                "Cheapest flight now {price} ({change} since {date}): {flight}.",
                locale,
                price=price,
                change=delta(report.cheapest_change, c),
                date=since,
                flight=flight,
            )
        ]
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
        params = change_params(report.drops[0], c, locale)
        lines.append(
            translate(
                "Biggest drop since {date}: {flight}, {change} to {price}.",
                locale,
                date=since,
                **params,
            )
        )
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


def trip_period_lines(report: TripReport, since: str, locale: str) -> list[str]:
    """A Trip's cheapest option now against `since` (a date, already formatted)."""
    c = report.currency
    if report.total is None:
        first = translate("No complete option in this poll.", locale)
    elif report.change is None:
        price = format_amount(report.total, c)
        first = translate("Cheapest option now {price}.", locale, price=price)
    else:
        first = translate(
            "Cheapest option now {price} ({change} since {date}).",
            locale,
            price=format_amount(report.total, c),
            change=delta(report.change, c),
            date=since,
        )
    return [first, translate("Working options: {n}.", locale, n=report.options)]


def digest_mail(
    user: User,
    since: str,
    overview: str | None,
    items: list[tuple[str, str | None, list[str]]],
    unchanged: list[str],
    settings_link: str | None,
) -> Mail:
    """`items`: (name, link, lines) of every tracked search or Trip that changed notably;
    `unchanged`: the names of the others."""
    locale = locale_of(user)
    blocks: list[str | None] = []
    if overview:
        note = translate("Written by AI from the numbers of the app.", locale)
        blocks.append(f"{overview}\n({note})")
    for name, link, lines in items:
        blocks.append(f"{name}\n{link}" if link else name)
        blocks.append("\n".join(f"• {line}" for line in lines))
    if unchanged:
        names = ", ".join(unchanged)
        blocks.append(translate("No notable change: {names}.", locale, names=names))
    if user.digest is DigestFrequency.WEEKLY:
        how = translate("You get this digest every Monday when prices changed notably.", locale)
    else:
        how = translate("You get this digest every morning when prices changed notably.", locale)
    if settings_link:
        how += "\n" + translate("To change or stop it:\n{link}", locale, link=settings_link)
    blocks.append(how)
    return Mail(
        user.email,
        translate("Price digest: what changed since {date}", locale, date=since),
        body(*blocks),
        kind="digest",
        user_id=user.id,
    )
