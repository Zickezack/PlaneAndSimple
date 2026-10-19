"""The admins' log of AI requests (`ai_usage`), written by `services/ai.py` for every request –
failed and cut-off ones too.

Each row keeps feature, user, model, tokens, duration and outcome (half a year) and, unless
switched off under Platform Settings → AI, the messages: what the app sent, thinking the
provider returned, the answer as received and as the app used it (30 days – a travel wish is
the user's own text). The API key is never part of it. The caller commits.
"""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, update
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.models import AIUsage
from flighttracker.services.ai_wire import AIError, Exchange

LOG_RETENTION = timedelta(days=183)
MESSAGE_RETENTION = timedelta(days=30)
MAX_TEXT = 50_000  # characters per stored message
MESSAGE_COLUMNS = ("system_prompt", "user_prompt", "thinking", "answer", "result")


@dataclass(frozen=True)
class UsageLog:
    """Where a request is logged; the caller commits."""

    session: Session
    feature: str  # draft, explain, test
    user_id: int | None = None


def record(
    settings: Settings,
    usage: UsageLog | None,
    exchange: Exchange,
    started: float,
    error: AIError | None = None,
) -> None:
    if usage is None:
        return
    key = settings.ai_api_key.get_secret_value() if settings.ai_api_key else ""

    def kept(text: str | None) -> str | None:
        if not settings.ai_log_messages or not text:
            return None
        text = text[:MAX_TEXT]
        return text.replace(key, "***") if key else text

    reply = exchange.reply
    usage.session.add(
        AIUsage(
            user_id=usage.user_id,
            feature=usage.feature,
            provider=settings.ai_provider or "-",
            model=(settings.ai_model or "-")[:120],
            input_tokens=reply.input_tokens if reply else None,
            output_tokens=reply.output_tokens if reply else None,
            output_limit=exchange.output_limit,
            duration_ms=int((time.monotonic() - started) * 1000),
            ok=error is None,
            cut_off=bool(reply and reply.cut_off),
            stop_reason=str(reply.stop)[:40] if reply and reply.stop else None,
            error=str(error) if error else None,
            request={"url": exchange.url, "body": exchange.params} if exchange.url else None,
            response=_without(reply.details, key) if reply else None,
            system_prompt=kept(exchange.system),
            user_prompt=kept(exchange.user),
            thinking=kept(exchange.thinking or (reply.thinking if reply else "")),
            answer=kept(reply.text if reply else error.received if error else None),
            result=kept(exchange.result),
        )
    )
    _prune(usage.session, datetime.now(UTC))
    usage.session.flush()


def _without(details: dict, key: str) -> dict | None:
    """The provider's response for the log (the key is not expected in it – and never kept)."""
    if not details:
        return None
    text = json.dumps(details, ensure_ascii=False, default=str)
    return json.loads(text.replace(key, "***")) if key and key in text else json.loads(text)


def _prune(session: Session, now: datetime) -> None:
    session.execute(delete(AIUsage).where(AIUsage.requested_at < now - LOG_RETENTION))
    stored = or_(*(getattr(AIUsage, column).is_not(None) for column in MESSAGE_COLUMNS))
    session.execute(
        update(AIUsage)
        .where(AIUsage.requested_at < now - MESSAGE_RETENTION, stored)
        .values(dict.fromkeys(MESSAGE_COLUMNS))
    )
