import ipaddress
from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, AnyHttpUrl, field_validator


def _validate_webhook_url(v: str) -> str:
    """Block SSRF — reject webhook URLs that target localhost or private IP literals.

    Only checks the literal hostname — no DNS lookup is performed, because:
    1. A synchronous DNS lookup blocks the async event loop and fails with
       "Network is unreachable" when DNS is unavailable.
    2. DNS-based SSRF prevention is already handled by the API endpoint using
       asyncio.to_thread, so duplicating it here is unnecessary.
    """
    try:
        hostname = urlparse(v).hostname or ""

        if hostname.lower() in ("localhost", "localhost."):
            raise ValueError("Webhook URL must not target localhost")

        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError(
                    f"Webhook URL targets a private/internal address ({hostname}). "
                    "Only public URLs are allowed."
                )
        except ValueError as ip_exc:
            if "private" in str(ip_exc) or "loopback" in str(ip_exc) or "internal" in str(ip_exc) or "localhost" in str(ip_exc):
                raise  # re-raise our own rejection
            # hostname is not an IP literal — that's fine, it's a normal domain
    except ValueError:
        raise
    except Exception:
        pass
    return v


class WebhookCreate(BaseModel):
    url: AnyHttpUrl
    events: list[str] = ["*"]
    secret: str | None = None

    @field_validator("url", mode="after")
    @classmethod
    def no_ssrf(cls, v: AnyHttpUrl) -> AnyHttpUrl:
        _validate_webhook_url(str(v))
        return v


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    is_active: bool
    created_at: datetime
    delivery_attempts: int = 0
    last_delivery_at: datetime | None = None
    last_delivery_status: int | None = None

    model_config = {"from_attributes": True}
