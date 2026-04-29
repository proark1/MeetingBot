"""Hashing helpers for opaque tokens (share links, support keys).

Old code used bare SHA-256 over the plaintext token, which meant a leaked DB
plus a leaked plaintext token (e.g. from access logs) gave instant correlation.
v2.41 switches new tokens to HMAC-SHA256 keyed with ``settings.JWT_SECRET`` —
this peppers the hash so a DB-only leak can't be linked to a plaintext.

To avoid breaking already-issued tokens, ``verify_token`` accepts both formats:

    "h2:<64-hex>"    — current format, HMAC-SHA256(JWT_SECRET, token)
    "<64-hex>"       — legacy bare-SHA-256 from earlier versions

Tokens themselves stay opaque base64url strings — these helpers operate on
their hashes only.
"""

from __future__ import annotations

import hashlib
import hmac

_HMAC_PREFIX = "h2:"


def _hmac_hash(plaintext: str, secret: str) -> str:
    return _HMAC_PREFIX + hmac.new(
        secret.encode(), plaintext.encode(), hashlib.sha256
    ).hexdigest()


def _legacy_hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def hash_token(plaintext: str, secret: str | None = None) -> str:
    """Return the canonical (current-format) hash for a new token."""
    if not secret:
        from app.config import settings
        secret = settings.JWT_SECRET
    return _hmac_hash(plaintext, secret)


def verify_token(plaintext: str, stored_hash: str | None, secret: str | None = None) -> bool:
    """Return True if ``plaintext`` matches ``stored_hash`` under either the
    HMAC-SHA256 (current) or bare-SHA-256 (legacy) hashing scheme."""
    if not stored_hash:
        return False
    if not secret:
        from app.config import settings
        secret = settings.JWT_SECRET
    if stored_hash.startswith(_HMAC_PREFIX):
        return hmac.compare_digest(stored_hash, _hmac_hash(plaintext, secret))
    return hmac.compare_digest(stored_hash, _legacy_hash(plaintext))


def hash_candidates(plaintext: str, secret: str | None = None) -> list[str]:
    """Return [current_hash, legacy_hash]. Use when you need to look up a row
    by hash (e.g. SQL `WHERE share_token_hash IN (...)`)."""
    if not secret:
        from app.config import settings
        secret = settings.JWT_SECRET
    return [_hmac_hash(plaintext, secret), _legacy_hash(plaintext)]
