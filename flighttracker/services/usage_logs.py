"""Read side of the admins' logs: mails (`mail_log`) and AI requests (`ai_usage`) – a summary
of the last 30 days and the latest entries, shown in the Mail and AI sections of Platform
Settings."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flighttracker.models import AIUsage, MailLog, User

PERIOD = timedelta(days=30)
LATEST = 25


@dataclass(frozen=True)
class MailEntry:
    entry: MailLog
    username: str | None


@dataclass(frozen=True)
class AIEntry:
    entry: AIUsage
    username: str | None


@dataclass(frozen=True)
class MailOverview:
    sent: int
    failed: int
    latest: list[MailEntry]


@dataclass(frozen=True)
class FeatureUsage:
    feature: str
    requests: int
    failed: int
    input_tokens: int
    output_tokens: int
    average_seconds: float


@dataclass(frozen=True)
class AIOverview:
    features: list[FeatureUsage]
    latest: list[AIEntry]

    @property
    def requests(self) -> int:
        return sum(f.requests for f in self.features)

    @property
    def failed(self) -> int:
        return sum(f.failed for f in self.features)

    @property
    def input_tokens(self) -> int:
        return sum(f.input_tokens for f in self.features)

    @property
    def output_tokens(self) -> int:
        return sum(f.output_tokens for f in self.features)


def mail_overview(session: Session, now: datetime) -> MailOverview:
    counts = dict(
        session.execute(
            select(MailLog.ok, func.count())
            .where(MailLog.sent_at >= now - PERIOD)
            .group_by(MailLog.ok)
        ).all()
    )
    rows = session.execute(
        select(MailLog, User.username)
        .outerjoin(User, User.id == MailLog.user_id)
        .order_by(MailLog.sent_at.desc(), MailLog.id.desc())
        .limit(LATEST)
    )
    return MailOverview(
        sent=counts.get(True, 0),
        failed=counts.get(False, 0),
        latest=[MailEntry(entry, username) for entry, username in rows],
    )


def ai_overview(session: Session, now: datetime) -> AIOverview:
    rows = session.execute(
        select(
            AIUsage.feature,
            func.count(),
            func.count().filter(AIUsage.ok.is_(False)),
            func.coalesce(func.sum(AIUsage.input_tokens), 0),
            func.coalesce(func.sum(AIUsage.output_tokens), 0),
            func.avg(AIUsage.duration_ms),
        )
        .where(AIUsage.requested_at >= now - PERIOD)
        .group_by(AIUsage.feature)
        .order_by(AIUsage.feature)
    )
    features = [
        FeatureUsage(feature, requests, failed, int(tokens_in), int(tokens_out), (ms or 0) / 1000)
        for feature, requests, failed, tokens_in, tokens_out, ms in rows
    ]
    latest = session.execute(
        select(AIUsage, User.username)
        .outerjoin(User, User.id == AIUsage.user_id)
        .order_by(AIUsage.requested_at.desc(), AIUsage.id.desc())
        .limit(LATEST)
    )
    return AIOverview(features, [AIEntry(entry, username) for entry, username in latest])
