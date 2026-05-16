from __future__ import annotations

import secrets
import string

import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# Characters used for admin-generated temporary passwords. Skips visually
# ambiguous chars (0/O, 1/l/I) so they're easy to read aloud or copy.
_TEMP_ALPHABET = "".join(
    c for c in string.ascii_letters + string.digits if c not in "0O1lI"
)


def generate_temp_password(length: int = 14) -> str:
    return "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))
