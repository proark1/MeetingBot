"""
Exceptions raised by the MeetingBot SDK.
"""
from __future__ import annotations


class MeetingBotError(Exception):
    """Base exception for all MeetingBot SDK errors."""

    def __init__(self, message: str, status_code: int | None = None, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(message={self.message!r}, "
            f"status_code={self.status_code!r}, detail={self.detail!r})"
        )


class AuthError(MeetingBotError):
    """Raised on HTTP 401 Unauthorized or 403 Forbidden responses."""


class NotFoundError(MeetingBotError):
    """Raised on HTTP 404 Not Found responses."""


class RateLimitError(MeetingBotError):
    """Raised on HTTP 429 Too Many Requests responses."""


class ServerError(MeetingBotError):
    """Raised on HTTP 5xx responses."""


class ValidationError(MeetingBotError):
    """Raised on HTTP 422 Unprocessable Entity responses."""
