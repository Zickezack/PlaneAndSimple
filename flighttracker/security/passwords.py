"""Password hashing with stdlib scrypt – no extra dependency needed for a single admin account.

Format `scrypt:n:r:p:salt:hash` (URL-safe base64) deliberately avoids `$`, which docker compose
would try to interpolate in env files.
"""

import base64
import hashlib
import hmac
import os

DEFAULT_N = 2**15
DEFAULT_R = 8
DEFAULT_P = 1
_MAXMEM = 128 * 1024 * 1024
_SCHEME = "scrypt"


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(
    password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=32)
    return f"{_SCHEME}:{n}:{r}:{p}:{_b64encode(salt)}:{_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split(":")
        if scheme != _SCHEME:
            return False
        expected = _b64decode(digest)
        candidate = hashlib.scrypt(
            password.encode(),
            salt=_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAXMEM,
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(candidate, expected)
