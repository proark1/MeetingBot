"""Optional error tracking / performance tracing via Sentry.

No-op unless ``SENTRY_DSN`` is set and the ``sentry-sdk`` package is installed,
so local/dev and minimal deployments are unaffected. Call :func:`init_sentry`
once, before the FastAPI app is created — the SDK auto-instruments Starlette/
FastAPI, asyncio, and logging without any further wiring.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def init_sentry() -> bool:
    """Initialise Sentry if configured. Returns True when enabled."""
    from app.config import settings

    dsn = (settings.SENTRY_DSN or "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logger.warning(
            "SENTRY_DSN is set but sentry-sdk is not installed — "
            "error tracking disabled. Add 'sentry-sdk' to requirements."
        )
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=settings.ENVIRONMENT,
            release=f"justheretolisten@{getattr(settings, 'APP_VERSION', '') or 'dev'}",
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            profiles_sample_rate=settings.SENTRY_PROFILES_SAMPLE_RATE,
            # Capture WARNING+ as breadcrumbs and ERROR+ as events.
            integrations=[
                LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR),
            ],
            # Never ship request bodies / headers that may contain secrets.
            send_default_pii=False,
        )
    except Exception as exc:  # never let observability setup crash boot
        logger.warning("Sentry init failed (continuing without it): %s", exc)
        return False

    logger.info("Sentry error tracking enabled (env=%s)", settings.ENVIRONMENT)
    return True
