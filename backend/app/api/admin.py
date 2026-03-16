"""Admin API — platform configuration management (wallet address, etc.)."""

import logging
import re
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_admin
from app.models.account import Account, MonitorState, PlatformConfig

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


class ManualCreditRequest(BaseModel):
    email: str = Field(description="Email of the account to credit.")
    amount_usd: float = Field(gt=0, description="Amount in USD to credit (must be positive).")
    note: Optional[str] = Field(None, description="Optional admin note recorded in the transaction description.")


class ManualCreditResponse(BaseModel):
    account_id: str
    email: str
    credited_usd: float
    new_balance_usd: float


class RescanRequest(BaseModel):
    from_block: int = Field(ge=0, description="Block number to start rescanning from.")


class RescanResponse(BaseModel):
    from_block: int
    message: str


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


@router.post("/credit", response_model=ManualCreditResponse)
async def manual_credit(
    payload: ManualCreditRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    Manually credit a user's account.

    Use this to correct missed USDC deposits that the monitor failed to detect
    (e.g. the user sent from an unregistered wallet, or the monitor was not
    running when the on-chain transfer occurred).
    """
    from app.services.credit_service import add_credits

    result = await db.execute(select(Account).where(Account.email == payload.email))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

    note = payload.note or "Manual admin credit"
    new_balance = await add_credits(
        account_id=account.id,
        amount_usd=Decimal(str(payload.amount_usd)),
        type="usdc_topup",
        description=f"Admin manual credit: {note}",
        reference_id=None,
        db=db,
    )
    logger.info(
        "Admin manually credited $%.4f to account %s (%s). Note: %s",
        payload.amount_usd, account.id, account.email, note,
    )
    return ManualCreditResponse(
        account_id=account.id,
        email=account.email,
        credited_usd=float(payload.amount_usd),
        new_balance_usd=float(new_balance),
    )


@router.post("/usdc/rescan", response_model=RescanResponse)
async def rescan_usdc_from_block(
    payload: RescanRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    Reset the USDC monitor's last-processed block so it will rescan from a
    given block on its next cycle.

    Use this when a USDC deposit was missed because the monitor had already
    advanced past that block (e.g. the user sent from an unregistered wallet
    and later registered it).  After calling this endpoint the monitor will
    re-examine all Transfer events starting at `from_block` on the next 60-second
    tick; duplicate transactions are skipped automatically via idempotency checks.
    """
    result = await db.execute(
        select(MonitorState).where(MonitorState.key == "usdc_last_block")
    )
    state = result.scalar_one_or_none()

    # Set last processed block to one before the desired start so the monitor
    # picks up from_block inclusive on its next run.
    new_value = str(max(0, payload.from_block - 1))
    if state is None:
        state = MonitorState(key="usdc_last_block", value=new_value)
        db.add(state)
    else:
        state.value = new_value

    await db.commit()
    logger.info("Admin reset USDC monitor last-block to %s (will rescan from block %d)", new_value, payload.from_block)
    return RescanResponse(
        from_block=payload.from_block,
        message=f"Monitor will rescan USDC transfers starting from block {payload.from_block} on the next cycle (≤60 s).",
    )
