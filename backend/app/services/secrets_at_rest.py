"""Encryption-at-rest helper for sensitive third-party tokens.

Used by ``Integration.config`` (Slack tokens, Notion API keys, Google Drive
access tokens, etc.). The column type stays ``Text``; the contents are now a
Fernet ciphertext (URL-safe base64, leading ``gAAAAA…``) instead of plain JSON.

Backward-compatible: ``decrypt_json`` first tries Fernet, then falls back to
parsing the value as plain JSON so rows written before this migration keep
working until they're updated and re-saved.

Key derivation: the Fernet key is the URL-safe base64 of
``SHA-256(JWT_SECRET || "integration-config-v1")``. This pins the secret to a
versioned label so we can rotate later without breaking old rows (we'd add a
v2 helper that tries v2 then v1 then plaintext). If ``JWT_SECRET`` is the
default ``"change-me-in-production"`` we deliberately refuse to encrypt — the
caller will get a ``RuntimeError`` and the integration config write fails
loudly rather than ship plaintext that masquerades as ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_INSECURE_DEFAULT_SECRET = "change-me-in-production"
_KEY_LABEL = b"integration-config-v1"
_FERNET_PREFIX = b"gAAAAA"


def _derive_key(secret: str) -> bytes:
    """Return a 32-byte URL-safe base64 Fernet key derived from JWT_SECRET."""
    digest = hashlib.sha256(secret.encode() + b":" + _KEY_LABEL).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet():
    from app.config import settings
    if settings.JWT_SECRET == _INSECURE_DEFAULT_SECRET:
        raise RuntimeError(
            "JWT_SECRET is the insecure default — refusing to encrypt "
            "integration config. Set JWT_SECRET before configuring integrations."
        )
    from cryptography.fernet import Fernet
    return Fernet(_derive_key(settings.JWT_SECRET))


def encrypt_json(payload: dict[str, Any]) -> str:
    """Serialise ``payload`` to JSON and Fernet-encrypt it for storage."""
    raw = json.dumps(payload).encode()
    return _get_fernet().encrypt(raw).decode()


def decrypt_json(stored: str | None) -> dict[str, Any]:
    """Decrypt+deserialise a stored config blob.

    Accepts both Fernet ciphertext (current format) and bare JSON
    (legacy plaintext rows). Returns ``{}`` on failure rather than raising,
    so a corrupted row doesn't 500 the whole integrations list page.
    """
    if not stored:
        return {}
    raw = stored.encode() if isinstance(stored, str) else stored

    if raw.startswith(_FERNET_PREFIX):
        try:
            f = _get_fernet()
            decoded = f.decrypt(raw)
            return json.loads(decoded.decode())
        except Exception as exc:
            logger.warning("decrypt_json: Fernet decryption failed (%s); returning empty config", exc)
            return {}

    # Legacy plaintext JSON path
    try:
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        logger.warning("decrypt_json: stored value is neither valid Fernet nor JSON; returning empty config")
        return {}
