"""Webhook signature verification helpers.

JustHereToListen.io signs every webhook delivery with HMAC-SHA256 over
``f"{timestamp}.{body}"``. The signature is sent in ``X-MeetingBot-Signature``
(``sha256=<hex>``) and the timestamp in ``X-MeetingBot-Timestamp`` (Unix seconds).

Receivers MUST:
1. Verify the signature with a constant-time compare.
2. Reject deliveries whose timestamp is older than ``max_age_seconds`` to
   prevent replay (default: 300 s).
"""

from __future__ import annotations

import hashlib
import hmac
import time

from .exceptions import MeetingBotError


class WebhookVerificationError(MeetingBotError):
    """Raised when a webhook signature or timestamp fails verification."""


def verify_webhook(
    body: str | bytes,
    timestamp: str | int,
    signature: str,
    secret: str,
    *,
    max_age_seconds: int = 300,
    now: float | None = None,
) -> None:
    """Verify a webhook delivery's signature and freshness.

    :param body: Raw request body (str or bytes), exactly as received.
    :param timestamp: ``X-MeetingBot-Timestamp`` header value (Unix seconds).
    :param signature: ``X-MeetingBot-Signature`` header value (``sha256=<hex>``).
    :param secret: Your webhook's signing secret.
    :param max_age_seconds: Maximum acceptable age in seconds (default: 300).
    :param now: Override the current time (for testing).

    :raises WebhookVerificationError: if the signature is invalid or the
        timestamp is missing, malformed, or outside the freshness window.
    """
    if not signature or not signature.startswith("sha256="):
        raise WebhookVerificationError("Missing or malformed signature header")
    try:
        ts_int = int(str(timestamp).strip())
    except (TypeError, ValueError) as exc:
        raise WebhookVerificationError(f"Invalid timestamp: {timestamp!r}") from exc

    current = time.time() if now is None else now
    if abs(current - ts_int) > max_age_seconds:
        raise WebhookVerificationError(
            f"Timestamp {ts_int} is outside the {max_age_seconds}s freshness window"
        )

    body_bytes = body.encode() if isinstance(body, str) else body
    signed_payload = f"{ts_int}.".encode() + body_bytes
    expected = "sha256=" + hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookVerificationError("Signature mismatch")
