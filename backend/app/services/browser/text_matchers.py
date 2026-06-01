"""Pure text-matching helpers for browser-bot state detection.

All functions take ``body_text`` (already lower-cased) and a ``platform``
key and return a bool.  Pure functions — no I/O, no Playwright dependency —
so they can be tested exhaustively without a real browser.

The underlying signal dictionaries live in ``platform_texts.py``.
"""

from app.services.browser.platform_texts import (
    IN_CALL_TEXTS,
    WAITING_TEXTS,
    END_TEXTS,
    ALONE_TEXTS,
)


def text_signals_in_call(body_text: str, platform: str) -> bool:
    """Return True if body text contains an in-call signal for the platform."""
    return any(t in body_text for t in IN_CALL_TEXTS.get(platform, []))


def text_signals_waiting(body_text: str, platform: str) -> bool:
    """Return True if body text contains a waiting-room signal."""
    return any(t in body_text for t in WAITING_TEXTS.get(platform, []))


def text_signals_ended(body_text: str, platform: str) -> bool:
    """Return True if body text contains a call-ended / removed signal."""
    return any(t in body_text for t in END_TEXTS.get(platform, []))


def text_signals_alone(body_text: str, platform: str) -> bool:
    """Return True if body text signals the bot is the only participant.

    When the platform has no ALONE_TEXTS (e.g. onepizza), returns False so
    the caller falls back to DOM tile-count alone.
    """
    signals = ALONE_TEXTS.get(platform, [])
    if not signals:
        return False
    return any(t in body_text for t in signals)
