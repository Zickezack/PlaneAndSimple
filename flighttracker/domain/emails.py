"""E-mail addresses: a deliberately simple check – the mail server has the last word."""

import re

MAX_EMAIL_LENGTH = 254
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")


def is_valid_email(value: str) -> bool:
    return len(value) <= MAX_EMAIL_LENGTH and bool(_EMAIL.match(value))
