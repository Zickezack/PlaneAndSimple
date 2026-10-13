"""Texts of the mails, in the recipient's language (English source texts, German catalog)."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from flighttracker.domain.currencies import currency_decimals
from flighttracker.i18n import DEFAULT_LOCALE, format_number, translate
from flighttracker.models import User
from flighttracker.services.mail import Mail

SIGNATURE = "– Plane and simple"


def locale_of(user: User) -> str:
    return user.locale or DEFAULT_LOCALE


def _body(*paragraphs: str | None) -> str:
    return "\n\n".join(p for p in (*paragraphs, SIGNATURE) if p) + "\n"


def format_amount(amount: Decimal, currency: str) -> str:
    return f"{format_number(amount, currency_decimals(currency))} {currency}"


def test_mail(to: str, locale: str) -> Mail:
    return Mail(
        to,
        translate("Test mail from Plane and simple", locale),
        _body(translate("Mail is set up correctly: this test mail arrived.", locale)),
        kind="test",
    )


def welcome_mail(
    user: User, login_link: str | None, password_link: str | None, valid_days: int
) -> Mail:
    """`password_link`: the user has no password yet and sets it there."""
    locale = locale_of(user)
    return Mail(
        user.email,
        translate("Your Plane and simple account", locale),
        _body(
            translate(
                "An account was created for you. Your username: {name}", locale, name=user.username
            ),
            translate(
                "Set your password here (the link works once, for {n} days):\n{link}",
                locale,
                n=valid_days,
                link=password_link,
            )
            if password_link
            else None,
            translate("Log in here:\n{link}", locale, link=login_link)
            if login_link and not password_link
            else None,
        ),
        kind="welcome",
        user_id=user.id,
    )


def reset_mail(user: User, link: str, valid_minutes: int) -> Mail:
    locale = locale_of(user)
    return Mail(
        user.email,
        translate("Reset your Plane and simple password", locale),
        _body(
            translate("Someone asked to reset the password of {name}.", locale, name=user.username),
            translate(
                "Set a new password here (the link works once, for {n} minutes):\n{link}",
                locale,
                n=valid_minutes,
                link=link,
            ),
            translate("If that was not you, ignore this mail – your password stays.", locale),
        ),
        kind="reset",
        user_id=user.id,
    )


def password_changed_mail(user: User, reset_link: str | None) -> Mail:
    locale = locale_of(user)
    return Mail(
        user.email,
        translate("Your Plane and simple password was changed", locale),
        _body(
            translate(
                "The password of {name} was just changed; all other sessions were logged out.",
                locale,
                name=user.username,
            ),
            translate(
                "If that was not you, set a new password right away:\n{link}",
                locale,
                link=reset_link,
            )
            if reset_link
            else translate("If that was not you, contact your admin right away.", locale),
        ),
        kind="password_changed",
        user_id=user.id,
    )


@dataclass(frozen=True)
class AlertMatch:
    origin: str
    destination: str
    cabin_class: str
    departure_date: date
    return_date: date | None
    price: Decimal
    currency: str
    max_price: Decimal


def _match_line(match: AlertMatch) -> str:
    dates = match.departure_date.isoformat()
    if match.return_date:
        dates += f" – {match.return_date.isoformat()}"
    cabin = match.cabin_class.replace("_", " ")
    price = format_amount(match.price, match.currency)
    return f"• {match.origin} → {match.destination}, {dates}, {cabin}: {price}"


def price_alert_mail(
    user: User, search_name: str, matches: list[AlertMatch], link: str | None
) -> Mail:
    locale = locale_of(user)
    cheapest = min(matches, key=lambda m: m.price)
    return Mail(
        user.email,
        translate(
            "Price alert: {name} from {price}",
            locale,
            name=search_name,
            price=format_amount(cheapest.price, cheapest.currency),
        ),
        _body(
            translate(
                'These flights of "{name}" are now at or below your alert price:',
                locale,
                name=search_name,
            ),
            "\n".join(_match_line(m) for m in sorted(matches, key=lambda m: m.price)),
            translate("All prices and flights:\n{link}", locale, link=link) if link else None,
            translate(
                "You get each flight again only when it gets cheaper. Change or delete the alert "
                "on the tracked search's page.",
                locale,
            ),
        ),
        kind="price_alert",
        user_id=user.id,
    )
