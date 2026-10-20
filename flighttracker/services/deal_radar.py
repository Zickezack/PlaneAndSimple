"""Deal radar: a mail when a poll finds unusually cheap prices (domain/deals.py) – to the owner
and everyone the tracked search is shared with, unless they switched these mails off.

Each flight is mailed once to a user, and again only when it gets cheaper than the price last
mailed (`deal_notifications`); it counts as mailed only once its mail went out. The caller
commits.
"""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.deals import Deal
from flighttracker.domain.poll_report import from_facts
from flighttracker.i18n import translate
from flighttracker.models import DealNotification, PollReport, Search, Share, User
from flighttracker.services import mail, mail_messages, report_texts

FlightKey = tuple[str, str, str, object, object]


def recipients(session: Session, search: Search) -> list[User]:
    """Owner and share users who are active, have an address and want these mails."""
    shared = select(Share.user_id).where(Share.search_id == search.id)
    return list(
        session.scalars(
            select(User)
            .where(
                or_(User.id == search.owner_id, User.id.in_(shared)),
                User.is_active.is_(True),
                User.email.is_not(None),
                User.deal_mails.is_(True),
            )
            .order_by(User.id)
        )
    )


def _key(deal: Deal) -> FlightKey:
    f = deal.flight
    return (f.origin, f.destination, f.cabin, f.departure, f.returning)


def _last_mailed(session: Session, search_id: int) -> dict[int, dict[FlightKey, Decimal]]:
    rows = session.execute(
        select(
            DealNotification.user_id,
            DealNotification.origin_iata,
            DealNotification.destination_iata,
            DealNotification.cabin_class,
            DealNotification.departure_date,
            DealNotification.return_date,
            func.min(DealNotification.price),
        )
        .where(DealNotification.search_id == search_id)
        .group_by(
            DealNotification.user_id,
            DealNotification.origin_iata,
            DealNotification.destination_iata,
            DealNotification.cabin_class,
            DealNotification.departure_date,
            DealNotification.return_date,
        )
    )
    mailed: dict[int, dict[FlightKey, Decimal]] = defaultdict(dict)
    for user_id, *key, price in rows:
        mailed[user_id][tuple(key)] = price
    return mailed


def _line(deal: Deal, currency: str, locale: str) -> str:
    return translate(
        "{flight}: {price} (usually {usual}, {percent} % less)",
        locale,
        flight=report_texts.flight_text(deal.flight, locale),
        price=mail_messages.format_amount(deal.flight.price, currency),
        usual=mail_messages.format_amount(deal.usual, currency),
        percent=deal.percent_below,
    )


def notify(
    session: Session, settings: Settings, search: Search, row: PollReport | None, now: datetime
) -> int:
    """Mails for the unusually cheap prices of this poll's report; returns the mails sent."""
    report = from_facts(row.facts) if row else None
    if report is None or not report.deals or not mail.mail_enabled(settings):
        return 0
    mailed = _last_mailed(session, search.id)
    links = (
        mail.link(settings, f"/searches/{search.id}"),
        mail.link(settings, "/settings#personal"),
    )
    sent = 0
    for user in recipients(session, search):
        known = mailed[user.id]
        fresh = [d for d in report.deals if _key(d) not in known or d.flight.price < known[_key(d)]]
        if not fresh:
            continue
        locale = mail_messages.locale_of(user)
        message = mail_messages.deal_mail(
            user,
            search.name,
            [_line(d, report.currency, locale) for d in fresh],
            mail_messages.format_amount(min(d.flight.price for d in fresh), report.currency),
            links,
            [block] if (block := report_texts.summary_block(row, locale)) else [],
        )
        if not mail.deliver(session, settings, message):
            continue
        sent += 1
        for deal in fresh:
            f = deal.flight
            session.add(
                DealNotification(
                    search_id=search.id,
                    user_id=user.id,
                    origin_iata=f.origin,
                    destination_iata=f.destination,
                    cabin_class=f.cabin,
                    departure_date=f.departure,
                    return_date=f.returning,
                    price=f.price,
                    currency=report.currency,
                    notified_at=now,
                )
            )
    session.flush()
    return sent
