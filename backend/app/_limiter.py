"""Shared slowapi rate-limiter instance.

Import this module instead of instantiating Limiter in individual routers
so that all routers share the same instance that is registered on app.state.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address


def _client_ip(request) -> str:
    """Return the client IP, honouring proxy headers only when trusted.

    Behind a reverse proxy (e.g. Railway) the socket peer is the proxy, so
    every client would share one rate-limit bucket. When ``TRUST_PROXY_HEADERS``
    is enabled we use the left-most ``X-Forwarded-For`` hop instead. When it's
    disabled we ignore the (spoofable) header and fall back to the socket peer.
    """
    try:
        from app.config import settings
        if settings.TRUST_PROXY_HEADERS:
            xff = request.headers.get("x-forwarded-for", "")
            if xff:
                return xff.split(",")[0].strip()
    except Exception:
        pass
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip)
