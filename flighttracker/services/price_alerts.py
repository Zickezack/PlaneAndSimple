"""Price alerts: a mail when flights of a Suchabo cost at most a user's price.

Optionally only for one route and/or cabin. After every completed poll of the Suchabo the
worker compares its current prices with the alerts (`notify`). A flight (route, cabin,
departure and return date) is reported once, and again only when it gets cheaper than the
price last reported – at most one mail per flight and price drop. One mail per user and poll
lists all flights that are new or cheaper.
"""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.filters import CabinClass
from flighttracker.domain.spec import route_pairs
from flighttracker.i18n import Msg
from flighttracker.models import PriceAlert, PriceAlertNotification, Search, User
from flighttracker.providers.registry import FAKE_DATA_PROVIDERS
from flighttracker.services import history, mail, mail_messages
from flighttracker.services.mail_messages import AlertMatch
from flighttracker.services.searches import spec_of

MAX_ALERTS_PER_SEARCH_AND_USER = 10

FlightKey = tuple[str, str, str, object, object]


class AlertValidationError(ValueError):
    def __init__(self, errors: list[Msg]):
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors


def alerts_of(session: Session, search_id: int, user_id: int) -> list[PriceAlert]:
    return list(
        session.scalars(
            select(PriceAlert)
            .where(PriceAlert.search_id == search_id, PriceAlert.user_id == user_id)
            .order_by(PriceAlert.id)
        )
    )


def create_alert(
    session: Session,
    search: Search,
    user: User,
    *,
    max_price: str,
    currency: str,
    route: str = "",
    cabin: str = "",
) -> PriceAlert:
    """`route`: "ZRH-BKK" or empty (all routes); `cabin`: a cabin class or empty (all)."""
    errors: list[Msg] = []
    if not user.email:
        errors.append(Msg("Add your e-mail address under Settings to get price alerts."))
    try:
        price = Decimal(max_price.replace("'", "").replace(",", ".").strip())
        if not price.is_finite() or price <= 0 or price >= Decimal("1e8"):
            raise InvalidOperation
    except InvalidOperation:
        errors.append(Msg("Enter the highest price as a positive number."))
        price = Decimal(0)
    spec = spec_of(search)
    origin = destination = None
    if route:
        origin, _, destination = route.partition("-")
        if (origin, destination) not in route_pairs(spec):
            errors.append(Msg("This route is not part of the tracked search."))
    if cabin and cabin not in {str(c) for c in spec.filters.cabin_classes}:
        errors.append(Msg("This cabin class is not part of the tracked search."))
    if len(alerts_of(session, search.id, user.id)) >= MAX_ALERTS_PER_SEARCH_AND_USER:
        errors.append(
            Msg("At most {n} alerts per tracked search.", n=MAX_ALERTS_PER_SEARCH_AND_USER)
        )
    if errors:
        raise AlertValidationError(errors)
    alert = PriceAlert(
        search_id=search.id,
        user_id=user.id,
        max_price=price,
        currency=currency,
        origin_iata=origin,
        destination_iata=destination,
        cabin_class=str(CabinClass(cabin)) if cabin else None,
    )
    session.add(alert)
    session.flush()
    return alert


def delete_alert(session: Session, search_id: int, alert_id: int, user_id: int) -> bool:
    """Only the user's own alert of this Suchabo."""
    alert = session.get(PriceAlert, alert_id)
    if alert is None or alert.search_id != search_id or alert.user_id != user_id:
        return False
    session.delete(alert)
    return True


def _key(origin, destination, cabin, departure, returning) -> FlightKey:
    return (origin, destination, str(cabin), departure, returning)


def _last_reported(session: Session, alert_ids: list[int]) -> dict[int, dict[FlightKey, Decimal]]:
    """Per alert and flight: the price of the latest notification."""
    latest = (
        select(func.max(PriceAlertNotification.id))
        .where(PriceAlertNotification.alert_id.in_(alert_ids))
        .group_by(
            PriceAlertNotification.alert_id,
            PriceAlertNotification.origin_iata,
            PriceAlertNotification.destination_iata,
            PriceAlertNotification.cabin_class,
            PriceAlertNotification.departure_date,
            PriceAlertNotification.return_date,
        )
    )
    reported: dict[int, dict[FlightKey, Decimal]] = defaultdict(dict)
    for row in session.scalars(
        select(PriceAlertNotification).where(PriceAlertNotification.id.in_(latest))
    ):
        key = _key(
            row.origin_iata,
            row.destination_iata,
            row.cabin_class,
            row.departure_date,
            row.return_date,
        )
        reported[row.alert_id][key] = row.price
    return reported


def matches(session: Session, search: Search, now: datetime) -> dict[PriceAlert, list[AlertMatch]]:
    """Per alert: the current, upcoming, real prices at or below it that were not reported
    yet at this price or a lower one."""
    alerts = list(session.scalars(select(PriceAlert).where(PriceAlert.search_id == search.id)))
    if not alerts:
        return {}
    filters = spec_of(search).filters
    points = history.price_points(
        session, search.id, FAKE_DATA_PROVIDERS, passengers=(filters.adults, filters.children)
    )
    seen = history.latest_poll_dates(session, search.id)
    today = now.date()
    current = [
        p
        for p in points
        if p.departure_date >= today and not p.fake and history.is_current(p, seen)
    ]
    reported = _last_reported(session, [alert.id for alert in alerts])
    result: dict[PriceAlert, list[AlertMatch]] = {}
    for alert in alerts:
        found = []
        for p in current:
            if (
                p.currency != alert.currency
                or p.price > alert.max_price
                or (alert.origin_iata and p.origin != alert.origin_iata)
                or (alert.destination_iata and p.destination != alert.destination_iata)
                or (alert.cabin_class and str(p.cabin_class) != alert.cabin_class)
            ):
                continue
            key = _key(p.origin, p.destination, p.cabin_class, p.departure_date, p.return_date)
            last = reported[alert.id].get(key)
            if last is not None and p.price >= last:
                continue
            found.append(
                AlertMatch(
                    p.origin,
                    p.destination,
                    str(p.cabin_class),
                    p.departure_date,
                    p.return_date,
                    p.price,
                    p.currency,
                    alert.max_price,
                )
            )
        if found:
            result[alert] = found
    return result


def notify(session: Session, settings: Settings, search: Search, now: datetime) -> int:
    """Mails to every user with new or cheaper matches; returns the number of mails sent.
    A flight counts as reported only once its mail went out (so a failed one is retried
    after the next poll)."""
    if not mail.mail_enabled(settings):
        return 0
    by_user: dict[User, list[tuple[PriceAlert, AlertMatch]]] = defaultdict(list)
    for alert, found in matches(session, search, now).items():
        if alert.user.is_active and alert.user.email:
            by_user[alert.user].extend((alert, match) for match in found)
    sent = 0
    link = mail.link(settings, f"/searches/{search.id}")
    for user, pairs in by_user.items():
        unique = {
            (m.origin, m.destination, m.cabin_class, m.departure_date, m.return_date): m
            for _, m in pairs
        }
        message = mail_messages.price_alert_mail(user, search.name, list(unique.values()), link)
        if not mail.deliver(session, settings, message):
            continue
        sent += 1
        for alert, match in pairs:
            session.add(
                PriceAlertNotification(
                    alert_id=alert.id,
                    origin_iata=match.origin,
                    destination_iata=match.destination,
                    cabin_class=match.cabin_class,
                    departure_date=match.departure_date,
                    return_date=match.return_date,
                    price=match.price,
                    currency=match.currency,
                    notified_at=now,
                )
            )
    session.flush()
    return sent
