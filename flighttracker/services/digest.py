"""Price digest: a daily or weekly mail per user (personal settings) about how the prices of
their tracked searches and Trips changed since the start of the period – sent at 07:00 in the
user's time zone (weekly on Mondays), and only when something changed notably. With AI set up
an overview in a few sentences comes on top (one request per mail). The caller commits.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain import trip_report
from flighttracker.domain.deals import unusual_prices
from flighttracker.domain.poll_report import moved, notable, search_report
from flighttracker.i18n import format_weekday_date
from flighttracker.models import (
    DigestFrequency,
    FetchJob,
    JobStatus,
    PollReport,
    Search,
    Share,
    Trip,
    TripLeg,
    User,
)
from flighttracker.services import ai, ai_summaries, digest_texts, mail, mail_messages
from flighttracker.services.poll_reports import observed_flights, route_text

SEND_HOUR = 7
PERIODS = {
    DigestFrequency.DAILY: timedelta(days=1),
    DigestFrequency.WEEKLY: timedelta(days=7),
}


def latest_slot(frequency: DigestFrequency, now: datetime, zone: ZoneInfo) -> datetime:
    """The latest send time at or before `now`: 07:00 local, weekly on a Monday (UTC)."""
    local = now.astimezone(zone)
    slot = local.replace(hour=SEND_HOUR, minute=0, second=0, microsecond=0)
    if frequency is DigestFrequency.WEEKLY:
        slot -= timedelta(days=slot.weekday())
    if slot > local:
        slot -= PERIODS[frequency]
    return slot.astimezone(UTC)


def due_since(user: User, now: datetime, default_zone: str) -> datetime | None:
    """Start of the period the user's digest is due for now, or None."""
    if user.digest is DigestFrequency.OFF or not user.email or not user.is_active:
        return None
    slot = latest_slot(user.digest, now, ZoneInfo(user.timezone or default_zone))
    if user.digest_at is not None and user.digest_at >= slot:
        return None
    return slot - PERIODS[user.digest]


def due_users(session: Session, settings: Settings, now: datetime) -> list[tuple[int, datetime]]:
    """(user id, start of the period) of every digest due now."""
    if not mail.mail_enabled(settings):
        return []
    users = session.scalars(
        select(User)
        .where(User.digest != DigestFrequency.OFF, User.email.is_not(None))
        .order_by(User.id)
    )
    due = []
    for user in users:
        since = due_since(user, now, settings.display_timezone)
        if since is not None:
            due.append((user.id, since))
    return due


def _visible(user: User, model) -> object:
    shared = select(Share.search_id if model is Search else Share.trip_id).where(
        Share.user_id == user.id
    )
    return or_(model.owner_id == user.id, model.id.in_(shared))


def _latest_poll_start(session: Session, search_id: int) -> datetime | None:
    return session.scalar(
        select(FetchJob.started_at)
        .where(
            FetchJob.search_id == search_id,
            FetchJob.trip_id.is_(None),
            FetchJob.status == JobStatus.DONE,
        )
        .order_by(FetchJob.finished_at.desc(), FetchJob.id.desc())
    )


def _trip_report_at(session: Session, trip_id: int, before: datetime | None, after=None):
    statement = select(PollReport).where(PollReport.trip_id == trip_id)
    if before is not None:
        statement = statement.where(PollReport.created_at < before)
    if after is not None:
        statement = statement.where(PollReport.created_at >= after)
    row = session.scalar(statement.order_by(PollReport.created_at.desc(), PollReport.id.desc()))
    return trip_report.from_facts(row.facts) if row else None


def _search_item(session, search, since, now, locale, since_text):
    started = _latest_poll_start(session, search.id)
    if started is None or started < since:
        return None  # not polled in the period
    currency, flights = observed_flights(
        session, search, now.date(), current_since=started, compare_before=since
    )
    report = search_report(currency, flights, None, unusual_prices(flights))
    lines = digest_texts.period_lines(report, since_text, locale)
    facts = ai_summaries.search_facts(search.name, route_text(search), report, since_text)
    return notable(report), lines, facts


def _trip_item(session, trip, since, locale, since_text):
    now_report = _trip_report_at(session, trip.id, None, after=since)
    if now_report is None:
        return None  # not polled in the period
    then = _trip_report_at(session, trip.id, since)
    report = trip_report.trip_report(
        now_report.currency, now_report.options, list(now_report.legs), then
    )
    changed = report.total is not None and report.total_before is not None
    worth = (changed and bool(moved(report.total, report.total_before))) or (
        report.options_before is not None and (report.options > 0) != (report.options_before > 0)
    )
    lines = digest_texts.trip_period_lines(report, since_text, locale)
    return worth, lines, ai_summaries.trip_facts(trip.name, report, since_text)


def build(
    session: Session, settings: Settings, user: User, since: datetime, now: datetime
) -> mail.Mail | None:
    """The digest mail for `user` since `since`; None when nothing changed notably."""
    locale = mail_messages.locale_of(user)
    zone = ZoneInfo(user.timezone or settings.display_timezone)
    since_text = format_weekday_date(since.astimezone(zone).date(), locale)
    legs = select(TripLeg.search_id)
    searches = session.scalars(
        select(Search)
        .where(Search.archived_at.is_(None), Search.id.not_in(legs), _visible(user, Search))
        .order_by(Search.name)
    )
    trips = session.scalars(
        select(Trip).where(Trip.archived_at.is_(None), _visible(user, Trip)).order_by(Trip.name)
    )
    items, unchanged, facts = [], [], []
    for search in searches:
        found = _search_item(session, search, since, now, locale, since_text)
        if found is not None:
            worth, lines, search_facts = found
            if worth:
                items.append((search.name, mail.link(settings, f"/searches/{search.id}"), lines))
                facts.append(search_facts)
            else:
                unchanged.append(search.name)
    for trip in trips:
        found = _trip_item(session, trip, since, locale, since_text)
        if found is not None:
            worth, lines, trip_facts = found
            if worth:
                items.append((trip.name, mail.link(settings, f"/trips/{trip.id}"), lines))
                facts.append(trip_facts)
            else:
                unchanged.append(trip.name)
    if not items:
        return None
    overview = _overview(session, settings, user, locale, since_text, facts, now)
    return digest_texts.digest_mail(
        user, since_text, overview, items, unchanged, mail.link(settings, "/settings#personal")
    )


def _overview(session, settings, user, locale, since_text, facts, now) -> str | None:
    """The AI's overview across the user's tracked searches and Trips, in their language."""
    if not (
        ai.ai_enabled(settings)
        and settings.ai_digest_overview
        and not ai_summaries.paused(session, now)
    ):
        return None
    try:
        return ai_summaries.summarize_digest(
            settings, since_text, facts, locale, ai.UsageLog(session, "digest", user.id)
        )
    except ai.AIError:
        return None


def mark_run(session: Session, user_id: int, now: datetime) -> None:
    """The period counts as done – before the mail is built, so a failure is not repeated every
    minute (it is in the logs)."""
    session.get(User, user_id).digest_at = now
    session.flush()


def send(
    session: Session, settings: Settings, user_id: int, since: datetime, now: datetime
) -> bool:
    user = session.get(User, user_id)
    message = build(session, settings, user, since, now)
    return message is not None and mail.deliver(session, settings, message)
