"""Encryption of credentials stored in the database (the SMTP password under Platform Settings).

Fernet (AES + HMAC) with a key derived from `SECRET_KEY`: a database dump alone does not reveal
the password. Changing `SECRET_KEY` makes stored values unreadable – they are then ignored and
must be entered again.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "fernet:"


def _fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(b"plane-and-simple/settings/" + secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str, secret_key: str) -> str:
    return _PREFIX + _fernet(secret_key).encrypt(value.encode()).decode()


def decrypt(stored: str, secret_key: str) -> str | None:
    """The plain value, or None if it was not encrypted with this key."""
    if not stored.startswith(_PREFIX):
        return None
    try:
        return _fernet(secret_key).decrypt(stored.removeprefix(_PREFIX).encode()).decode()
    except InvalidToken:
        return None
