"""Prometheus metrics endpoint.

Exposes operational metrics at GET /metrics (unauthenticated).
Metrics are also updated via FastAPI middleware for every HTTP request.

Usage:
    curl http://localhost:8000/metrics
"""

import time
import logging
from typing import Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Ops"])

# ── Prometheus registry ────────────────────────────────────────────────────────

try:
    from prometheus_client import (
        Counter, Histogram, Gauge, CollectorRegistry,
        generate_latest, CONTENT_TYPE_LATEST,
        REGISTRY as _DEFAULT_REGISTRY,
    )
    _PROM_AVAILABLE = True
    _REGISTRY = _DEFAULT_REGISTRY
except ImportError:
    _PROM_AVAILABLE = False
    _REGISTRY = None
    logger.warning("prometheus_client not installed — /metrics will return a stub response")


def _make_counter(name, doc, labelnames=()):
    if not _PROM_AVAILABLE:
        return None
    try:
        from prometheus_client import Counter as _Counter
        return _Counter(name, doc, labelnames)
    except Exception:
        return None


def _make_histogram(name, doc, labelnames=(), buckets=None):
    if not _PROM_AVAILABLE:
        return None
    try:
        from prometheus_client import Histogram as _Histogram
        kwargs = {"labelnames": labelnames}
        if buckets:
            kwargs["buckets"] = buckets
        return _Histogram(name, doc, **kwargs)
    except Exception:
        return None


def _make_gauge(name, doc, labelnames=()):
    if not _PROM_AVAILABLE:
        return None
    try:
        from prometheus_client import Gauge as _Gauge
        return _Gauge(name, doc, labelnames)
    except Exception:
        return None


# ── Metrics definitions ────────────────────────────────────────────────────────

# HTTP request metrics
http_requests_total = _make_counter(
    "meetingbot_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)
http_request_duration_seconds = _make_histogram(
    "meetingbot_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# Bot lifecycle metrics
bots_created_total = _make_counter(
    "meetingbot_bots_created_total",
    "Total bots created",
    ["platform"],
)
bots_active = _make_gauge(
    "meetingbot_bots_active",
    "Number of bots currently active (joining/in_call/transcribing)",
)
bots_completed_total = _make_counter(
    "meetingbot_bots_completed_total",
    "Total bots that reached terminal state",
    ["status"],  # done | error | cancelled
)

# AI token usage metrics
ai_tokens_total = _make_counter(
    "meetingbot_ai_tokens_total",
    "Total AI tokens consumed",
    ["operation", "provider"],
)
ai_cost_usd_total = _make_counter(
    "meetingbot_ai_cost_usd_total",
    "Total AI cost in USD",
    ["provider"],
)

# Webhook delivery metrics
webhook_deliveries_total = _make_counter(
    "meetingbot_webhook_deliveries_total",
    "Total webhook delivery attempts",
    ["status"],  # success | retrying | failed
)


# ── Helper functions (called from services) ───────────────────────────────────

def record_bot_created(platform: str) -> None:
    if bots_created_total:
        try:
            bots_created_total.labels(platform=platform).inc()
        except Exception:
            pass


def record_bot_completed(status: str) -> None:
    if bots_completed_total:
        try:
            bots_completed_total.labels(status=status).inc()
        except Exception:
            pass


def record_ai_usage(operation: str, provider: str, tokens: int, cost_usd: float) -> None:
    if ai_tokens_total:
        try:
            ai_tokens_total.labels(operation=operation, provider=provider).inc(tokens)
        except Exception:
            pass
    if ai_cost_usd_total:
        try:
            ai_cost_usd_total.labels(provider=provider).inc(cost_usd)
        except Exception:
            pass


def record_webhook_delivery(status: str) -> None:
    if webhook_deliveries_total:
        try:
            webhook_deliveries_total.labels(status=status).inc()
        except Exception:
            pass


def update_active_bots(count: int) -> None:
    if bots_active:
        try:
            bots_active.set(count)
        except Exception:
            pass


# ── Middleware ─────────────────────────────────────────────────────────────────

class PrometheusMiddleware(BaseHTTPMiddleware):
    """Track HTTP request counts and latencies."""

    # Paths to skip (avoid recording metrics for the metrics endpoint itself)
    _SKIP_PATHS = {"/metrics", "/health"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        method = request.method

        # Skip metrics collection for health/metrics endpoints to avoid cardinality noise
        if path in self._SKIP_PATHS:
            return await call_next(request)

        # Normalise path — replace UUIDs to avoid high cardinality
        import re
        norm_path = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "{id}",
            path,
        )

        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        status_code = str(response.status_code)

        if http_requests_total:
            try:
                http_requests_total.labels(
                    method=method, path=norm_path, status_code=status_code
                ).inc()
            except Exception:
                pass

        if http_request_duration_seconds:
            try:
                http_request_duration_seconds.labels(
                    method=method, path=norm_path
                ).observe(duration)
            except Exception:
                pass

        return response


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("/metrics", include_in_schema=False)
async def get_metrics():
    """Prometheus metrics endpoint (unauthenticated).

    Returns metrics in the Prometheus text exposition format.
    Scrape with:
        - url: http://your-host:8000/metrics
    """
    if not _PROM_AVAILABLE:
        return PlainTextResponse(
            "# prometheus_client not installed\n"
            "# Install with: pip install prometheus-client>=0.19.0\n",
            media_type="text/plain",
        )

    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        output = generate_latest()
        return Response(content=output, media_type=CONTENT_TYPE_LATEST)
    except Exception as exc:
        logger.error("Failed to generate Prometheus metrics: %s", exc)
        return PlainTextResponse(f"# error generating metrics: {exc}\n", status_code=500)
