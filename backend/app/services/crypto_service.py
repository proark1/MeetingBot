"""USDC/ERC-20 deposit address generation and transfer monitoring.

Supports two modes:
  1. **Platform wallet mode** — admin sets a single collection wallet via the
     admin panel. Users register their own Ethereum wallet on their account.
     The monitor matches the `from` address of incoming transfers to user wallets.
  2. **HD wallet mode** (legacy) — each user gets a unique deposit address
     derived from CRYPTO_HD_SEED. The monitor matches `to` addresses.

Requires:
  - CRYPTO_RPC_URL — Infura/Alchemy JSON-RPC endpoint
  - Either a platform wallet (set via admin) or CRYPTO_HD_SEED for HD mode
  - eth-account and web3 packages (only if HD mode is used)
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

    # Assign next available HD index (retry on race condition with concurrent requests)
    from sqlalchemy.exc import IntegrityError
    for attempt in range(5):
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
        try:
            await db.commit()
            break
        except IntegrityError:
            await db.rollback()
            if attempt == 4:
                raise
            # Another request may have claimed this index; check if our account got one
            result = await db.execute(
                select(UsdcDeposit).where(UsdcDeposit.account_id == account_id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                return existing.deposit_address

    logger.info("Created USDC deposit address for account %s: %s (index=%d)", account_id, address, next_index)
    return address


async def _get_platform_wallet() -> Optional[str]:
    """Return the admin-configured platform wallet address, or None."""
    from app.db import AsyncSessionLocal
    from app.models.account import PlatformConfig
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PlatformConfig).where(PlatformConfig.key == "usdc_collection_wallet")
        )
        config = result.scalar_one_or_none()
        return config.value if config and config.value else None


async def _get_rpc_url() -> Optional[str]:
    """Return the RPC URL from env var, falling back to the admin-configured DB value."""
    from app.config import settings
    if settings.CRYPTO_RPC_URL:
        return settings.CRYPTO_RPC_URL
    # Fall back to admin-set value stored in PlatformConfig
    from app.db import AsyncSessionLocal
    from app.models.account import PlatformConfig
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PlatformConfig).where(PlatformConfig.key == "crypto_rpc_url")
        )
        config = result.scalar_one_or_none()
        return config.value if config and config.value else None


async def test_rpc_url(url: str) -> tuple[bool, str]:
    """Test whether an RPC URL is reachable and returns a valid response.

    Makes a lightweight ``eth_blockNumber`` JSON-RPC call.  Returns
    ``(True, "")`` on success or ``(False, reason)`` on failure.
    """
    import json
    try:
        import httpx
    except ImportError:
        # Fall back to requests if httpx is not installed
        try:
            import requests as _requests  # type: ignore

            try:
                resp = _requests.post(
                    url,
                    json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
                    timeout=10,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                if "result" in data:
                    return True, ""
                if "error" in data:
                    return False, f"RPC error: {data['error'].get('message', data['error'])}"
                return False, "Unexpected RPC response format"
            except Exception as exc:
                return False, str(exc)
        except ImportError:
            logger.warning("Neither httpx nor requests available — skipping RPC URL test")
            return True, ""

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            if "result" in data:
                return True, ""
            if "error" in data:
                return False, f"RPC error: {data['error'].get('message', data['error'])}"
            return False, "Unexpected RPC response format"
    except Exception as exc:
        return False, str(exc)


async def start_usdc_monitor() -> None:
    """Start the background USDC transfer monitoring task.

    Always starts the loop regardless of env config — the RPC URL is resolved
    dynamically each cycle so it can be set via the admin panel without a restart.
    """
    logger.info("Starting USDC transfer monitor (polling every 60s)")
    asyncio.create_task(_monitor_loop())


async def _monitor_loop() -> None:
    backoff_s = 60
    while True:
        try:
            await _check_transfers()
            backoff_s = 60  # reset on success
        except Exception as exc:
            logger.error("USDC monitor error (retry in %ds): %s", backoff_s, exc, exc_info=True)
            backoff_s = min(backoff_s * 2, 3600)  # exponential backoff, cap at 1 hour
        await asyncio.sleep(backoff_s)


async def _check_transfers() -> None:
    from app.config import settings
    from app.db import AsyncSessionLocal
    from app.models.account import Account, UsdcDeposit, MonitorState, CreditTransaction, UnmatchedUsdcTransfer
    from app.services.credit_service import add_credits
    from sqlalchemy import select

    rpc_url = await _get_rpc_url()
    if not rpc_url:
        logger.warning(
            "USDC monitor: CRYPTO_RPC_URL is not set — monitoring is disabled. "
            "Set it as an environment variable or via the admin panel."
        )
        return

    async with AsyncSessionLocal() as db:
        platform_wallet = None
        # Check for platform wallet
        from app.models.account import PlatformConfig
        pw_result = await db.execute(
            select(PlatformConfig).where(PlatformConfig.key == "usdc_collection_wallet")
        )
        pw_config = pw_result.scalar_one_or_none()
        if pw_config and pw_config.value:
            platform_wallet = pw_config.value.lower()

        # Build lookup maps for both modes
        # Mode 1: Platform wallet — match `from` address to user's registered wallet
        from_addr_to_account = {}
        if platform_wallet:
            acct_result = await db.execute(
                select(Account).where(Account.wallet_address.isnot(None))
            )
            for acct in acct_result.scalars().all():
                from_addr_to_account[acct.wallet_address.lower()] = acct.id

        # Mode 2: HD addresses — match `to` address to per-user deposit addresses
        to_addr_to_account = {}
        if settings.CRYPTO_HD_SEED:
            dep_result = await db.execute(select(UsdcDeposit))
            for d in dep_result.scalars().all():
                to_addr_to_account[d.deposit_address.lower()] = d.account_id

        if not from_addr_to_account and not to_addr_to_account:
            logger.warning(
                "USDC monitor: no user wallets registered and no HD deposit addresses — "
                "nothing to match against. Register a wallet via PUT /api/v1/auth/wallet."
            )
            return

        logger.debug(
            "USDC monitor: platform_wallet=%s, %d registered sender wallet(s), %d HD address(es)",
            platform_wallet, len(from_addr_to_account), len(to_addr_to_account),
        )

        # Get last processed block
        state_result = await db.execute(
            select(MonitorState).where(MonitorState.key == "usdc_last_block")
        )
        state = state_result.scalar_one_or_none()

        # Collect the to-addresses we care about so the RPC query is filtered.
        # Without this filter, get_logs returns ALL USDC transfers on mainnet
        # (thousands per block), which blows past RPC result-size limits and
        # causes the monitor to fail without ever crediting anyone.
        filter_to_addresses: list[str] = []
        if platform_wallet:
            filter_to_addresses.append(platform_wallet)  # already lowercased
        filter_to_addresses.extend(to_addr_to_account.keys())  # HD deposit addrs

        last_block_val = int(state.value) if state and state.value else None
        logger.info(
            "USDC monitor: scanning from block %s, watching %d address(es): %s",
            last_block_val or "latest-1000",
            len(filter_to_addresses),
            filter_to_addresses,
        )

        # Poll blockchain in thread to avoid blocking event loop
        try:
            from_block, to_block, raw_events = await asyncio.to_thread(
                _fetch_usdc_events,
                rpc_url,
                settings.USDC_CONTRACT,
                last_block_val,
                filter_to_addresses or None,
            )
        except Exception as exc:
            logger.error("USDC RPC error: %s", exc, exc_info=True)
            return

        logger.info(
            "USDC monitor: scanned blocks %d–%d, found %d matching transfer(s)",
            from_block, to_block, len(raw_events),
        )

        for event in raw_events:
            to_addr = event["to"].lower()
            from_addr = event["from"].lower()
            tx_hash = event["tx_hash"]
            amount_usd = Decimal(str(event["value"])) / _USDC_DECIMALS

            account_id = None

            # Mode 1: Transfer TO platform wallet FROM a registered user wallet
            if platform_wallet and to_addr == platform_wallet and from_addr in from_addr_to_account:
                account_id = from_addr_to_account[from_addr]

            # Mode 2: Transfer TO a per-user HD deposit address
            if account_id is None and to_addr in to_addr_to_account:
                account_id = to_addr_to_account[to_addr]

            if account_id is None:
                # Transfer arrived at the platform wallet from an unrecognized address.
                # Record it so admins can identify the sender and manually credit their account.
                if platform_wallet and to_addr == platform_wallet:
                    dup = await db.execute(
                        select(UnmatchedUsdcTransfer).where(UnmatchedUsdcTransfer.tx_hash == tx_hash)
                    )
                    if dup.scalar_one_or_none() is None:
                        db.add(UnmatchedUsdcTransfer(
                            tx_hash=tx_hash,
                            from_address=from_addr,
                            to_address=to_addr,
                            amount_usdc=amount_usd,
                            block_number=event["block"],
                        ))
                        logger.warning(
                            "USDC transfer to platform wallet not attributed — "
                            "sender wallet not registered on any account. "
                            "Amount: %.6f USDC, from: %s, tx: %s. "
                            "Use POST /api/v1/admin/credit to manually credit the user, "
                            "or ask the user to register their wallet then use "
                            "POST /api/v1/admin/usdc/rescan.",
                            amount_usd, from_addr, tx_hash,
                        )
                continue

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
                description=f"USDC deposit: {amount_usd:.2f} USDC (from {from_addr[:10]}...)",
                reference_id=tx_hash,
                db=db,
            )
            logger.info(
                "USDC deposit credited: +$%.2f to account %s (tx=%s, from=%s)",
                amount_usd, account_id, tx_hash[:16], from_addr[:10],
            )

        # Persist last processed block
        if state is None:
            state = MonitorState(key="usdc_last_block", value=str(to_block))
            db.add(state)
        else:
            state.value = str(to_block)
        await db.commit()


def _fetch_usdc_events(
    rpc_url: str,
    contract_address: str,
    from_block: Optional[int],
    to_addresses: Optional[list] = None,
) -> tuple:
    """Synchronous: fetch USDC Transfer events via w3.eth.get_logs (runs in thread pool).

    Uses the raw JSON-RPC eth_getLogs call with explicit topic filters instead of
    the contract-event API.  This avoids web3.py version-specific Python keyword
    argument differences (fromBlock vs from_block changed between v5 and v7) and
    ensures the ``to``-address filter is applied at the RPC level so only relevant
    transfers are returned (not every USDC transfer on mainnet).
    """
    try:
        from web3 import Web3
    except ImportError:
        raise RuntimeError("web3 package not installed. Run: pip install web3")

    w3 = Web3(Web3.HTTPProvider(rpc_url))

    if not w3.is_connected():
        raise RuntimeError(f"Cannot connect to Ethereum RPC endpoint: {rpc_url}")

    current_block = w3.eth.block_number

    if from_block is None:
        from_block = max(0, current_block - 1000)  # Look back ~1000 blocks on first run

    if from_block >= current_block:
        return from_block, current_block, []

    # ERC-20 Transfer(address,address,uint256) event signature hash (topic[0])
    # This is a well-known constant; computing it avoids any version dependency.
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    # Build topic[2] filter (the `to` indexed parameter).
    # Ethereum topics for addresses are zero-padded to 32 bytes:
    #   "0x" + 24 zero chars + 40 address hex chars  (total 66 chars)
    to_topic: Optional[object] = None
    if to_addresses:
        padded = [
            "0x" + addr.replace("0x", "").lower().zfill(64)
            for addr in to_addresses
        ]
        to_topic = padded[0] if len(padded) == 1 else padded

    # topics array: [event_sig, any_from_address, to_filter]
    # None at position 1 means "match any from-address"
    topics = [TRANSFER_TOPIC, None, to_topic] if to_topic is not None else [TRANSFER_TOPIC]

    # Fetch in chunks of 2000 blocks to stay within RPC result limits
    all_logs = []
    chunk_size = 2000
    scan_from = from_block + 1
    while scan_from <= current_block:
        scan_to = min(scan_from + chunk_size - 1, current_block)
        filter_params = {
            "address": Web3.to_checksum_address(contract_address),
            "fromBlock": scan_from,
            "toBlock": scan_to,
            "topics": topics,
        }
        chunk_logs = w3.eth.get_logs(filter_params)
        all_logs.extend(chunk_logs)
        logger.debug(
            "USDC scan blocks %d–%d: %d event(s)", scan_from, scan_to, len(chunk_logs)
        )
        scan_from = scan_to + 1

    # Parse raw log entries into a normalised dict format.
    # topic[1] = from-address (indexed), topic[2] = to-address (indexed)
    # data     = ABI-encoded uint256 transfer value
    parsed = []
    for log in all_logs:
        try:
            log_topics = log.get("topics", [])
            if len(log_topics) < 3:
                continue  # malformed — skip
            from_addr = "0x" + log_topics[1].hex()[-40:]
            to_addr   = "0x" + log_topics[2].hex()[-40:]
            # data is a 32-byte big-endian uint256
            value     = int.from_bytes(bytes(log["data"]), "big") if log["data"] else 0
            tx_hash   = "0x" + log["transactionHash"].hex()
            block_num = log["blockNumber"]
            parsed.append({
                "from":    from_addr,
                "to":      to_addr,
                "value":   value,
                "tx_hash": tx_hash,
                "block":   block_num,
            })
        except Exception as exc:
            logger.warning("Failed to parse USDC log entry: %s", exc)
            continue

    return from_block, current_block, parsed
