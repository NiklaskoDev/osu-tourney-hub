"""Calendar feed tokens: random, shown once, stored only as a SHA-256 hash.

A token is a bearer secret in a URL, so it is long enough to be unguessable and a leaked database
does not reveal working links. (A fast hash is right for a 256-bit random value; the slow hashes
are for human passwords.)
"""

import hashlib
import secrets

TOKEN_BYTES = 32


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
