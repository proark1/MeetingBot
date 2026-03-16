"""USDC/ERC-20 deposit address generation and transfer monitoring.

Requires:
  - CRYPTO_HD_SEED (64-char hex) — master seed for HD wallet derivation
  - CRYPTO_RPC_URL — Infura/Alchemy JSON-RPC endpoint
  - eth-account and web3 packages

If CRYPTO_RPC_URL or CRYPTO_HD_SEED is not set, this module is effectively
a no-op (deposit addresses can still be generated but monitoring is disabled).
"""

import asyncio
import hashlib
import hmac
import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# Minimal ABI for the ERC-20 Transfer event
_ERC20_TRANSFER_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    }
]

# USDC has 6 decimal places
_USDC_DECIMALS = Decimal("1000000")


def derive_address(seed_hex: str, index: int) -> str:
    """
    Derive a deterministic Ethereum address from a hex seed and index.
    Uses HMAC-SHA256 to generate a private key, then derives the address.
    """
    try:
        from eth_account import Account as EthAccount
    except ImportError:
        raise RuntimeError("eth-account package not installed. Run: pip install eth-account")

    seed_bytes = bytes.fromhex(seed_hex)
    key_bytes = hmac.new(seed_bytes, f"usdc_deposit:{index}".encode(), hashlib.sha256).digest()
    account = EthAccount.from_key(key_bytes)
    return account.address


async def get_or_create_deposit_address(
    account_id: str,
    db,  # AsyncSession
) -> str:
    """
    Return the USDC deposit address for an account, creating one if needed.
    Raises RuntimeError if CRYPTO_HD_SEED is not configured.
    """
    from app.config import settings
    if not settings.CRYPTO_HD_SEED:
        raise RuntimeError(
            "CRYPTO_HD_SEED is not configured. "
            "Generate a random 64-char hex seed and set it as an environment variable."
        )

    from sqlalchemy import select, func
    from app.models.account import UsdcDeposit
    import uuid

    result = await db.execute(
        select(UsdcDeposit).where(UsdcDeposit.account_id == account_id)
    )
    deposit = result.scalar_one_or_none()

    if deposit:
        return deposit.deposit_address

    # Assign next available HD index
    max_result = await db.execute(select(func.max(UsdcDeposit.hd_index)))
    max_index = max_result.scalar_one_or_none()
    next_index = (max_index or -1) + 1

    address = derive_address(settings.CRYPTO_HD_SEED, next_index)

    deposit = UsdcDeposit(
        id=str(uuid.uuid4()),
        account_id=account_id,
        deposit_address=address,
        hd_index=next_index,
    )
    db.add(deposit)
    await db.commit()

    logger.info("Created USDC deposit address for account %s: %s (index=%d)", account_id, address, next_index)
    return address


async def start_usdc_monitor() -> None:
    """Start the background USDC transfer monitoring task."""
    from app.config import settings
    if not settings.CRYPTO_RPC_URL:
        logger.info("USDC monitoring disabled — CRYPTO_RPC_URL not set")
        return
    if not settings.CRYPTO_HD_SEED:
        logger.info("USDC monitoring disabled — CRYPTO_HD_SEED not set")
        return

    logger.info("Starting USDC transfer monitor (polling every 60s)")
    asyncio.create_task(_monitor_loop())


async def _monitor_loop() -> None:
    while True:
        try:
            await _check_transfers()
        except Exception as exc:
            logger.error("USDC monitor error: %s", exc, exc_info=True)
        await asyncio.sleep(60)


async def _check_transfers() -> None:
    from app.config import settings
    from app.db import AsyncSessionLocal
    from app.models.account import UsdcDeposit, MonitorState, CreditTransaction
    from app.services.credit_service import add_credits
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        # Load all registered deposit addresses
        dep_result = await db.execute(select(UsdcDeposit))
        deposits = dep_result.scalars().all()
        if not deposits:
            return

        addr_to_account = {d.deposit_address.lower(): d.account_id for d in deposits}

        # Get last processed block
        state_result = await db.execute(
            select(MonitorState).where(MonitorState.key == "usdc_last_block")
        )
        state = state_result.scalar_one_or_none()

        # Poll blockchain in thread to avoid blocking event loop
        try:
            from_block, to_block, raw_events = await asyncio.to_thread(
                _fetch_usdc_events,
                settings.CRYPTO_RPC_URL,
                settings.USDC_CONTRACT,
                int(state.value) if state and state.value else None,
            )
        except Exception as exc:
            logger.error("USDC RPC error: %s", exc)
            return

        for event in raw_events:
            to_addr = event["to"].lower()
            if to_addr not in addr_to_account:
                continue

            account_id = addr_to_account[to_addr]
            tx_hash = event["tx_hash"]
            amount_usd = Decimal(str(event["value"])) / _USDC_DECIMALS

            # Idempotency — skip if already processed
            dup = await db.execute(
                select(CreditTransaction).where(CreditTransaction.reference_id == tx_hash)
            )
            if dup.scalar_one_or_none():
                continue

            await add_credits(
                account_id=account_id,
                amount_usd=amount_usd,
                type="usdc_topup",
                description=f"USDC deposit: {amount_usd:.2f} USDC",
                reference_id=tx_hash,
                db=db,
            )
            logger.info(
                "USDC deposit credited: +$%.2f to account %s (tx=%s)",
                amount_usd, account_id, tx_hash[:16],
            )

        # Persist last processed block
        if state is None:
            state = MonitorState(key="usdc_last_block", value=str(to_block))
            db.add(state)
        else:
            state.value = str(to_block)
        await db.commit()


def _fetch_usdc_events(rpc_url: str, contract_address: str, from_block: Optional[int]) -> tuple:
    """Synchronous: fetch USDC Transfer events via web3.py (runs in thread pool)."""
    try:
        from web3 import Web3
    except ImportError:
        raise RuntimeError("web3 package not installed. Run: pip install web3")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    current_block = w3.eth.block_number

    if from_block is None:
        from_block = max(0, current_block - 1000)  # Look back ~1000 blocks on first run

    if from_block >= current_block:
        return from_block, current_block, []

    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(contract_address),
        abi=_ERC20_TRANSFER_ABI,
    )

    # Fetch in chunks of 2000 blocks to avoid RPC limits
    all_events = []
    chunk_size = 2000
    scan_from = from_block + 1
    while scan_from <= current_block:
        scan_to = min(scan_from + chunk_size - 1, current_block)
        events = usdc.events.Transfer().get_logs(fromBlock=scan_from, toBlock=scan_to)
        all_events.extend(events)
        scan_from = scan_to + 1

    parsed = [
        {
            "to": e["args"]["to"],
            "from": e["args"]["from"],
            "value": e["args"]["value"],
            "tx_hash": e["transactionHash"].hex(),
            "block": e["blockNumber"],
        }
        for e in all_events
    ]
    return from_block, current_block, parsed
