"""Admin API — platform configuration management (wallet address, etc.)."""

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_admin
from app.models.account import PlatformConfig

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(require_admin)])

_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Platform config keys
WALLET_KEY = "usdc_collection_wallet"


# ── Schemas ───────────────────────────────────────────────────────────────────

class WalletResponse(BaseModel):
    wallet_address: Optional[str] = Field(
        description="The platform USDC collection wallet address (ERC-20). Null if not set."
    )


class WalletUpdateRequest(BaseModel):
    wallet_address: str = Field(
        description="Ethereum address (0x..., 42 characters) where users send USDC.",
        examples=["0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"],
    )


class PlatformConfigItem(BaseModel):
    key: str
    value: str
    updated_at: Optional[str] = None


class PlatformConfigResponse(BaseModel):
    configs: list[PlatformConfigItem]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/wallet", response_model=WalletResponse)
async def get_platform_wallet(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Get the current platform USDC collection wallet address."""
    result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == WALLET_KEY)
    )
    config = result.scalar_one_or_none()
    return WalletResponse(wallet_address=config.value if config else None)


@router.put("/wallet", response_model=WalletResponse)
async def set_platform_wallet(
    payload: WalletUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Set or update the platform USDC collection wallet address."""
    address = payload.wallet_address.strip()

    if not _ETH_ADDRESS_RE.match(address):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid Ethereum address. Must be 0x followed by 40 hex characters.",
        )

    result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == WALLET_KEY)
    )
    config = result.scalar_one_or_none()

    if config:
        config.value = address
    else:
        config = PlatformConfig(key=WALLET_KEY, value=address)
        db.add(config)

    await db.commit()
    logger.info("Platform USDC wallet updated to %s", address)
    return WalletResponse(wallet_address=address)


@router.get("/config", response_model=PlatformConfigResponse)
async def get_all_platform_config(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Get all platform configuration values."""
    result = await db.execute(select(PlatformConfig))
    configs = result.scalars().all()
    return PlatformConfigResponse(
        configs=[
            PlatformConfigItem(
                key=c.key,
                value=c.value,
                updated_at=c.updated_at.isoformat() if c.updated_at else None,
            )
            for c in configs
        ]
    )
