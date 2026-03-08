import ipaddress
import socket
from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, AnyHttpUrl, field_validator


def _validate_webhook_url(v: str) -> str:
    """Block SSRF — reject webhook URLs that resolve to private/loopback addresses."""
    try:
        hostname = urlparse(v).hostname or ""
        results = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in results:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError(
                    f"Webhook URL resolves to a private/internal address ({sockaddr[0]}). "
                    "Only public URLs are allowed."
                )
    except ValueError:
        raise
    except Exception:
        pass  # DNS failure — let delivery fail naturally rather than blocking registration
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
