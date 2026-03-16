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
from app.models.account import Account, MonitorState, PlatformConfig, UnmatchedUsdcTransfer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(require_admin)])

_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Platform config keys
WALLET_KEY = "usdc_collection_wallet"
RPC_URL_KEY = "crypto_rpc_url"


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


class UnmatchedTransferItem(BaseModel):
    tx_hash: str = Field(description="Ethereum transaction hash.")
    from_address: str = Field(description="Sender's Ethereum address (unrecognized — not registered on any account).")
    to_address: str = Field(description="Platform wallet address that received the USDC.")
    amount_usdc: float = Field(description="Amount of USDC sent.")
    block_number: int = Field(description="Ethereum block number of the transfer.")
    detected_at: str = Field(description="ISO-8601 UTC timestamp when the monitor detected the transfer.")
    resolved: bool = Field(description="True if an admin has manually credited the account.")
    resolution_note: Optional[str] = Field(default=None, description="Admin note set when resolving.")


class UnmatchedTransferListResponse(BaseModel):
    transfers: list[UnmatchedTransferItem]
    total: int


class ResolveUnmatchedRequest(BaseModel):
    note: Optional[str] = Field(None, description="Optional note describing the resolution action taken.")


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


class RpcUrlRequest(BaseModel):
    rpc_url: str = Field(
        description="Ethereum JSON-RPC endpoint URL (Infura, Alchemy, QuickNode, etc.).",
        examples=["https://mainnet.infura.io/v3/YOUR_KEY"],
    )


class RpcUrlResponse(BaseModel):
    rpc_url_set: bool = Field(description="True if an RPC URL is now configured.")
    source: str = Field(description="'env' if from environment variable, 'db' if set via admin panel.")


@router.get("/rpc-url", response_model=RpcUrlResponse)
async def get_rpc_url_status(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Check whether a CRYPTO_RPC_URL is configured (env var or admin-set)."""
    from app.config import settings
    if settings.CRYPTO_RPC_URL:
        return RpcUrlResponse(rpc_url_set=True, source="env")
    result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == RPC_URL_KEY)
    )
    config = result.scalar_one_or_none()
    return RpcUrlResponse(
        rpc_url_set=bool(config and config.value),
        source="db" if config and config.value else "none",
    )


@router.put("/rpc-url", response_model=RpcUrlResponse)
async def set_rpc_url(
    payload: RpcUrlRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    Set the Ethereum RPC URL used by the USDC monitor.

    This stores the URL in the database so the monitor can use it without
    requiring a server restart or environment variable change.
    The environment variable `CRYPTO_RPC_URL` always takes precedence if set.
    """
    url = payload.rpc_url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="RPC URL must start with http:// or https://",
        )

    from app.services.crypto_service import test_rpc_url
    ok, reason = await test_rpc_url(url)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"RPC URL validation failed: {reason}",
        )

    result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == RPC_URL_KEY)
    )
    config = result.scalar_one_or_none()
    if config:
        config.value = url
    else:
        config = PlatformConfig(key=RPC_URL_KEY, value=url)
        db.add(config)
    await db.commit()
    logger.info("Admin set CRYPTO_RPC_URL via admin panel (stored in DB)")
    return RpcUrlResponse(rpc_url_set=True, source="db")


@router.get("/usdc/unmatched", response_model=UnmatchedTransferListResponse)
async def list_unmatched_transfers(
    resolved: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    List USDC transfers that arrived at the platform wallet but could not be
    attributed to any user account because the sender's wallet was not registered.

    These represent funds that were received on-chain but have not yet been credited
    to any user balance. To resolve:

    1. Identify the user by their `from_address` (ask them which wallet they used).
    2. Credit their account via `POST /admin/credit`.
    3. Mark the transfer resolved via `POST /admin/usdc/unmatched/{tx_hash}/resolve`.

    Use `?resolved=false` (default) to see only pending items, `?resolved=true` for
    resolved ones, or omit the parameter to see all.
    """
    from sqlalchemy import desc

    query = select(UnmatchedUsdcTransfer)
    if resolved is not None:
        query = query.where(UnmatchedUsdcTransfer.resolved == resolved)
    query = query.order_by(desc(UnmatchedUsdcTransfer.detected_at))

    result = await db.execute(query)
    transfers = result.scalars().all()

    return UnmatchedTransferListResponse(
        transfers=[
            UnmatchedTransferItem(
                tx_hash=t.tx_hash,
                from_address=t.from_address,
                to_address=t.to_address,
                amount_usdc=float(t.amount_usdc),
                block_number=t.block_number,
                detected_at=t.detected_at.isoformat(),
                resolved=t.resolved,
                resolution_note=t.resolution_note,
            )
            for t in transfers
        ],
        total=len(transfers),
    )


@router.post("/usdc/unmatched/{tx_hash}/resolve", response_model=UnmatchedTransferItem)
async def resolve_unmatched_transfer(
    tx_hash: str,
    payload: ResolveUnmatchedRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    Mark an unmatched USDC transfer as resolved after the account has been manually credited.

    Call this after using `POST /admin/credit` to apply the funds to the correct account.
    """
    result = await db.execute(
        select(UnmatchedUsdcTransfer).where(UnmatchedUsdcTransfer.tx_hash == tx_hash)
    )
    transfer = result.scalar_one_or_none()
    if transfer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found.")

    transfer.resolved = True
    transfer.resolution_note = payload.note or "Resolved by admin"
    await db.commit()
    logger.info("Admin marked unmatched USDC transfer %s as resolved. Note: %s", tx_hash, transfer.resolution_note)

    return UnmatchedTransferItem(
        tx_hash=transfer.tx_hash,
        from_address=transfer.from_address,
        to_address=transfer.to_address,
        amount_usdc=float(transfer.amount_usdc),
        block_number=transfer.block_number,
        detected_at=transfer.detected_at.isoformat(),
        resolved=transfer.resolved,
        resolution_note=transfer.resolution_note,
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
