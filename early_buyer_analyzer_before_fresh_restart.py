import json
import os
import shutil
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


# =========================================================
# STAGE 2 — FAST FIRST-ENTRY / FIRST-EXIT ANALYSER
# =========================================================
#
# Reads:
#   scanner_qualifying_tokens.json
#
# Preserves and skips tokens already saved in:
#   early_trader_results.json
#
# Saves:
#   early_trader_results.json
#   top_early_traders.json
#   early_trader_checkpoint.json
#
# For each token:
#   1. Finds the first 20 genuine DEX buyers.
#   2. Uses each buyer's already-known first buy as the entry.
#   3. Ignores every later incoming token transfer/additional buy.
#   4. Checks only outgoing transfers after the entry.
#   5. Stops at the first confirmed sale.
#   6. Calculates first-exit proceeds, proportional cost basis,
#      realised profit, ROI and current remaining balance.
# =========================================================


# =========================================================
# SETTINGS
# =========================================================

CHAIN_ID = 4663

# Blockscout Universal PRO API.
BLOCKSCOUT_API_KEY = os.getenv("BLOCKSCOUT_API_KEY", "").strip()
PRO_REST_BASE_URL = f"https://api.blockscout.com/{CHAIN_ID}"
PRO_LEGACY_API_URL = "https://api.blockscout.com/v2/api"

RPC_URL = "https://rpc.mainnet.chain.robinhood.com"

INPUT_FILE = Path("scanner_qualifying_tokens.json")
FULL_OUTPUT_FILE = Path("early_trader_results.json")
TOP_OUTPUT_FILE = Path("top_early_traders.json")
CHECKPOINT_FILE = Path("early_trader_checkpoint.json")
CHECKPOINT_DIR = Path("early_trader_checkpoints")
TX_CACHE_FILE = Path("early_transaction_cache.json")
INTERNAL_CACHE_FILE = Path("early_internal_cache.json")
SALE_PROBE_CACHE_FILE = Path("early_rpc_sale_probe_cache.json")

EARLY_BUYER_LIMIT = 15
TOP_TRADERS_PER_TOKEN = 5

# Only wallets that realised at least this much ETH profit qualify.
MIN_REALISED_PROFIT_ETH = 2.0

# Stop once approximately the whole original early-buy allocation
# has been sold. Allows for transfer-tax and rounding differences.
ORIGINAL_POSITION_EXIT_FRACTION = 0.95

# 0 means analyse every unprocessed token.
MAX_NEW_TOKENS_PER_RUN = 0

# Explicit force-recheck list. Keep empty during normal operation.
# A token listed here is reanalysed even if a completed report already exists.
FORCE_RECHECK_TOKEN_SYMBOLS: set[str] = set()

# Targeted exclusion: ProjectVex (VEX) repeatedly returns Blockscout HTTP 500
# errors and can block the rest of the queue. All other tokens keep the same
# retry and analysis logic.
# VEX is a known API-problem token.
# The remaining symbols are deliberately abandoned from the previous
# unfinished scanner batch and must not be retried by Stage 2.
# VEX remains excluded by symbol because it is a known API-problem token.
SKIP_TOKEN_SYMBOLS = {"VEX"}

# Exact contracts deliberately abandoned from the previous unfinished batch.
# Using contracts prevents an unrelated future token with the same symbol
# from being accidentally skipped.
SKIP_TOKEN_CONTRACTS = {
    "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9",  # AAPL
    "0x79bbf4508b1391af3a0f4b30bb5fc4aa9ab0e07c",  # ANON
    "0x451b42a15100c340ca12f7c66de06fac5ea2d751",  # BOW
    "0x77b0aa38451ccdc1b42587e2f80b9879a7f82356",  # DOGO
    "0x5ab3d4c385b400f3abb49e80de2faf6a88a7b691",  # FLOCK
    "0xc9a981fee1f9dec688bb123ccdecc63d0debfc4e",  # GLD
    "0xc72f232a6869e6cf34dc06129affd07f8a2a246a",  # MANCER
    "0x4d3f37a965b21ab4122e92dd41d2693e742c883b",  # RIPE
    "0xb8fa8010833463aac5595b55b9045479239eff79",  # WTH
}

# Historical tokens whose profitable-wallet reports were accidentally lost
# when an older build rebuilt top_early_traders.json from a reduced result set.
# These are added to the Stage 1 input in-memory only. Once each token is
# successfully analysed, its completed report is saved and future runs skip it.
RECOVERY_TOKENS = [
    {
        "name": "Cash Cat",
        "symbol": "CASHCAT",
        "contract": "0x020bfc650a365f8bb26819deaabf3e21291018b4",
    },
    {
        "name": "Arrow",
        "symbol": "ARROW",
        "contract": "0xf2915d1e3c1b0c769d0c756ec43f1c1f6c99cd03",
    },
    {
        "name": "up",
        "symbol": "UP",
        "contract": "0x57c0e45cb534413d1c20a4240955d6bb250bb4f1",
    },
    {
        "name": "Hoodrat",
        "symbol": "HOODRAT",
        "contract": "0x8e62f281f282686fca6dcb39288069a93fc23f1c",
    },
    {
        "name": "Sherwood Protocol",
        "symbol": "WOOD",
        "contract": "0xf8bc08092c06db6148114dcf82af881f1085f92b",
    },
]

PAGE_SIZE = 1_000
MAX_TOKEN_PAGES = 5
MAX_WALLET_PAGES = 10

MAX_RETRIES = 4
REQUEST_TIMEOUT = 30

# Blockscout's public API rate-limits rapid sequential requests. Every HTTP
# request is spaced out, and a Retry-After value of 0 can never bypass the
# minimum backoff.
REQUEST_DELAY = 0.75
TOKEN_DELAY = 3.0
MIN_RETRY_DELAY = 10
RETRY_DELAYS = (10, 20, 40, 60)

ZERO_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

ETH_SYMBOLS = {"ETH", "WETH"}

STABLE_SYMBOLS = {
    "USDC",
    "USDT",
    "DAI",
    "USDE",
    "USDG",
    "SYRUPUSDG",
}

SWAP_SELECTORS = {
    "0x7ff36ab5",
    "0xfb3bdb41",
    "0x38ed1739",
    "0x8803dbee",
    "0x18cbafe5",
    "0x4a25d94a",
    "0x414bf389",
    "0xc04b8d59",
    "0xdb3e2198",
    "0xf28c0498",
    "0x3593564c",
    "0x24856bc3",
    "0xac9650d8",
    "0x5ae401dc",
}

TX_CACHE: dict[str, dict[str, Any]] = {}
INTERNAL_CACHE: dict[str, list[dict[str, Any]]] = {}
SALE_PROBE_CACHE: dict[str, dict[str, Any]] = {}
RPC_TOKEN_METADATA_CACHE: dict[str, dict[str, Any]] = {}
RPC_BLOCK_TIMESTAMP_CACHE: dict[int, str | None] = {}
ADDRESS_CACHE: dict[str, dict[str, Any]] = {}

FAILED_TX_HASHES: set[str] = set()
FAILED_INTERNAL_HASHES: set[str] = set()

# Incremented whenever an API/RPC request permanently fails after retries.
# Token and trader analysis compare this counter before and after their work,
# so unavailable API data is never mistaken for a genuine empty result.
API_FAILURE_COUNT = 0
API_FAILURE_LOG: list[dict[str, Any]] = []
LAST_HTTP_REQUEST_AT = 0.0


# =========================================================
# BASIC HELPERS
# =========================================================

def safe_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, str) and value.startswith("0x"):
            return int(value, 16)

        return int(value or 0)

    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)

    except (TypeError, ValueError):
        return default


def normalise_address(value: Any) -> str:
    if isinstance(value, str):
        return value.lower()

    if isinstance(value, dict):
        return str(value.get("hash") or "").lower()

    return ""


def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None

    text = str(value).strip()

    if text.isdigit():
        try:
            return datetime.fromtimestamp(
                int(text),
                timezone.utc,
            )

        except (ValueError, OSError):
            return None

    try:
        result = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )

        if result.tzinfo is None:
            result = result.replace(
                tzinfo=timezone.utc
            )

        return result

    except ValueError:
        return None


def readable_time(value: Any) -> str:
    parsed = parse_time(value)

    return (
        parsed.isoformat()
        if parsed is not None
        else str(value or "Unknown")
    )


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "Unknown"

    total = max(0, int(seconds))

    if total < 60:
        return f"{total}s"

    minutes, seconds_left = divmod(
        total,
        60,
    )

    if minutes < 60:
        return (
            f"{minutes}m {seconds_left}s"
            if seconds_left
            else f"{minutes}m"
        )

    hours, minutes_left = divmod(
        minutes,
        60,
    )

    if hours < 24:
        return (
            f"{hours}h {minutes_left}m"
            if minutes_left
            else f"{hours}h"
        )

    days, hours_left = divmod(
        hours,
        24,
    )

    return (
        f"{days}d {hours_left}h"
        if hours_left
        else f"{days}d"
    )


def format_number(value: Any) -> str:
    number = safe_float(value)

    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"

    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"

    if abs(number) >= 1_000:
        return f"{number / 1_000:.2f}K"

    if abs(number) >= 1:
        return (
            f"{number:,.6f}"
            .rstrip("0")
            .rstrip(".")
        )

    return (
        f"{number:.12f}"
        .rstrip("0")
        .rstrip(".")
    )


TOP_BACKUP_CREATED = False


def backup_top_wallet_database() -> None:
    """Create a timestamped backup before this run first modifies the top file."""
    global TOP_BACKUP_CREATED

    if TOP_BACKUP_CREATED or not TOP_OUTPUT_FILE.exists():
        return

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%d_%H%M%S")

    backup_path = Path(
        f"top_early_traders_backup_{timestamp}.json"
    )

    shutil.copy2(
        TOP_OUTPUT_FILE,
        backup_path,
    )

    TOP_BACKUP_CREATED = True

    print(
        f"Cumulative wallet database backup created: "
        f"{backup_path}"
    )


def write_json(
    path: Path,
    data: Any,
) -> None:
    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary_path.write_text(
        json.dumps(
            data,
            indent=4,
        ),
        encoding="utf-8",
    )

    temporary_path.replace(path)


def read_json(
    path: Path,
    default: Any,
) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except (
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(
            f"Could not read {path}: {error}"
        )
        return default


# =========================================================
# NETWORK HELPERS
# =========================================================

def register_api_failure(
    description: str,
    reason: str,
) -> None:
    global API_FAILURE_COUNT

    API_FAILURE_COUNT += 1
    API_FAILURE_LOG.append({
        "time_utc": utc_now(),
        "description": description,
        "reason": reason,
    })


def throttle_http_request() -> None:
    """Keep all Blockscout HTTP requests at least REQUEST_DELAY apart."""
    global LAST_HTTP_REQUEST_AT

    now = time.monotonic()
    remaining = REQUEST_DELAY - (now - LAST_HTTP_REQUEST_AT)

    if remaining > 0:
        time.sleep(remaining)

    LAST_HTTP_REQUEST_AT = time.monotonic()


def request_json(
    url: str,
    params: dict[str, Any] | None = None,
    description: str = "Request",
) -> Any:
    """
    Make a Blockscout request with enforced pacing and safe backoff.

    None means the request could not be completed after all retries. Callers
    must treat that as incomplete API data, not as an empty result.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        wait_time = RETRY_DELAYS[
            min(attempt - 1, len(RETRY_DELAYS) - 1)
        ]

        try:
            throttle_http_request()

            request_params = dict(params or {})
            request_params["apikey"] = BLOCKSCOUT_API_KEY

            if url == PRO_LEGACY_API_URL:
                request_params["chain_id"] = CHAIN_ID

            response = requests.get(
                url,
                params=request_params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in (401, 403):
                message = (
                    f"authentication failed with HTTP "
                    f"{response.status_code}"
                )
                print(
                    f"{description}: Blockscout PRO authentication failed "
                    f"(HTTP {response.status_code}). Check the API key."
                )
                register_api_failure(description, message)
                return None

            if response.status_code == 404 and "api.blockscout.com" in url:
                message = "PRO route or chain unsupported (HTTP 404)"
                print(
                    f"{description}: Blockscout PRO returned HTTP 404. "
                    "The route or Robinhood Chain 4663 may not be supported."
                )
                register_api_failure(description, message)
                return None

            if response.status_code == 429:
                header_delay = safe_int(
                    response.headers.get("Retry-After"),
                    wait_time,
                )
                retry_after = max(
                    MIN_RETRY_DELAY,
                    wait_time,
                    header_delay,
                )

                if attempt == MAX_RETRIES:
                    message = (
                        f"rate limited after {MAX_RETRIES} attempts"
                    )
                    print(
                        f"{description} remained rate limited. "
                        "Marking this analysis incomplete."
                    )
                    register_api_failure(description, message)
                    return None

                print(
                    f"{description} rate limited "
                    f"({attempt}/{MAX_RETRIES}). "
                    f"Retrying in {retry_after}s..."
                )
                time.sleep(retry_after)
                continue

            if response.status_code >= 500:
                if attempt == MAX_RETRIES:
                    message = f"server error {response.status_code}"
                    print(
                        f"{description} server error "
                        f"{response.status_code}. "
                        "Marking this analysis incomplete."
                    )
                    register_api_failure(description, message)
                    return None

                print(
                    f"{description} server error "
                    f"{response.status_code} "
                    f"({attempt}/{MAX_RETRIES}). "
                    f"Retrying in {wait_time}s..."
                )
                time.sleep(wait_time)
                continue

            response.raise_for_status()
            return response.json()

        except (requests.RequestException, ValueError) as error:
            if attempt == MAX_RETRIES:
                print(
                    f"{description} failed after {MAX_RETRIES} "
                    f"attempts: {error}. "
                    "Marking this analysis incomplete."
                )
                register_api_failure(description, str(error))
                return None

            print(
                f"{description} error "
                f"({attempt}/{MAX_RETRIES}): {error}. "
                f"Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)

    register_api_failure(description, "unknown request failure")
    return None


def rpc_call(
    method: str,
    params: list[Any] | None = None,
) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or [],
    }

    for attempt in range(1, MAX_RETRIES + 1):
        wait_time = RETRY_DELAYS[
            min(attempt - 1, len(RETRY_DELAYS) - 1)
        ]

        try:
            response = requests.post(
                RPC_URL,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            if (
                response.status_code == 429
                or response.status_code >= 500
            ):
                if attempt < MAX_RETRIES:
                    time.sleep(wait_time)

                continue

            response.raise_for_status()
            data = response.json()

            if "error" not in data:
                return data.get("result")

        except (
            requests.RequestException,
            ValueError,
        ):
            pass

        if attempt < MAX_RETRIES:
            time.sleep(wait_time)

    return None


# =========================================================
# INPUT AND RESUME LOGIC
# =========================================================

def load_scanner_tokens_from_file() -> list[dict[str, Any]]:
    data = read_json(
        INPUT_FILE,
        {},
    )

    if isinstance(data, dict):
        tokens = data.get("tokens") or []
    elif isinstance(data, list):
        tokens = data
    else:
        tokens = []

    valid_tokens = []

    for token in tokens:
        if not isinstance(token, dict):
            continue

        contract = str(
            token.get("contract")
            or token.get("address_hash")
            or ""
        ).lower()

        if (
            contract.startswith("0x")
            and len(contract) == 42
        ):
            valid_tokens.append(
                {
                    **token,
                    "contract": contract,
                }
            )

    return valid_tokens


def load_scanner_tokens() -> list[dict[str, Any]]:
    """
    Load current Stage 1 tokens plus one-time historical recovery tokens.

    Contract address is the identity key, so duplicates are automatically
    collapsed. Recovery tokens stop being analysed once a completed report
    for their contract exists.
    """
    scanner_tokens = load_scanner_tokens_from_file()

    by_contract: dict[str, dict[str, Any]] = {}

    for token in scanner_tokens:
        contract = normalise_address(
            token.get("contract")
        )

        if contract:
            token = dict(token)
            token["contract"] = contract
            by_contract[contract] = token

    for recovery_token in RECOVERY_TOKENS:
        contract = normalise_address(
            recovery_token.get("contract")
        )

        if not contract:
            continue

        # Prefer richer Stage 1 scanner data if the token is already present.
        if contract not in by_contract:
            token = dict(recovery_token)
            token["contract"] = contract
            by_contract[contract] = token

    return list(by_contract.values())


def report_contract(
    report: dict[str, Any],
) -> str:
    token = report.get("token") or {}

    return str(
        token.get("contract") or ""
    ).lower()



def report_is_complete(
    report: dict[str, Any],
) -> bool:
    """Return True only for reports that are safe to treat as completed."""
    explicit_status = report.get("token_analysis_complete")

    if explicit_status is not None:
        return explicit_status is True

    # Backward compatibility for successful reports written by older builds.
    # Old reports did not have token_analysis_complete, so retain them unless
    # they explicitly contain an incomplete trader result.
    traders = report.get("ranked_traders") or []

    if not isinstance(traders, list):
        return False

    return all(
        not isinstance(trader, dict)
        or trader.get("analysis_complete", True) is True
        for trader in traders
    )


def load_existing_reports() -> list[dict[str, Any]]:
    data = read_json(
        FULL_OUTPUT_FILE,
        {},
    )

    if not isinstance(data, dict):
        return []

    reports = data.get("reports") or []

    return [
        report
        for report in reports
        if isinstance(report, dict)
        and report_contract(report)
    ]


def merge_reports(
    reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_contract: dict[
        str,
        dict[str, Any],
    ] = {}

    for report in reports:
        contract = report_contract(report)

        if contract:
            by_contract[contract] = report

    return list(by_contract.values())


def pending_tokens(
    scanner_tokens: list[dict[str, Any]],
    completed_contracts: set[str],
) -> list[dict[str, Any]]:
    pending = [
        token
        for token in scanner_tokens
        if token["contract"]
        not in completed_contracts
    ]

    if MAX_NEW_TOKENS_PER_RUN > 0:
        return pending[
            :MAX_NEW_TOKENS_PER_RUN
        ]

    return pending


# =========================================================
# PERSISTENT API CACHE / PER-TOKEN RESUME
# =========================================================

def load_persistent_api_caches() -> None:
    """Load successful Blockscout and RPC sale-probe results from earlier runs."""
    tx_data = read_json(TX_CACHE_FILE, {})
    internal_data = read_json(INTERNAL_CACHE_FILE, {})
    sale_probe_data = read_json(SALE_PROBE_CACHE_FILE, {})

    if isinstance(tx_data, dict):
        TX_CACHE.update({
            str(k).lower(): v
            for k, v in tx_data.items()
            if isinstance(v, dict)
        })

    if isinstance(internal_data, dict):
        INTERNAL_CACHE.update({
            str(k).lower(): v
            for k, v in internal_data.items()
            if isinstance(v, list)
        })

    if isinstance(sale_probe_data, dict):
        SALE_PROBE_CACHE.update({
            str(k): v
            for k, v in sale_probe_data.items()
            if isinstance(v, dict)
        })

    print(
        f"Persistent API cache loaded: {len(TX_CACHE)} Blockscout transactions + "
        f"{len(INTERNAL_CACHE)} internal results + "
        f"{len(SALE_PROBE_CACHE)} RPC sale probes."
    )


def save_persistent_api_caches() -> None:
    """Persist successful responses so retries never pay for them again."""
    write_json(TX_CACHE_FILE, TX_CACHE)
    write_json(INTERNAL_CACHE_FILE, INTERNAL_CACHE)
    write_json(SALE_PROBE_CACHE_FILE, SALE_PROBE_CACHE)


def token_checkpoint_path(contract: str) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    safe_contract = str(contract or "unknown").lower().replace("/", "_")
    return CHECKPOINT_DIR / f"{safe_contract}.json"


def load_token_checkpoint(contract: str) -> dict[str, Any]:
    data = read_json(token_checkpoint_path(contract), {})
    return data if isinstance(data, dict) else {}

# =========================================================
# BLOCKSCOUT DATA
# =========================================================

def fetch_token_info(
    contract: str,
) -> dict[str, Any]:
    data = request_json(
        f"{PRO_REST_BASE_URL}/api/v2/tokens/{contract}",
        description="Token info",
    )

    return (
        data
        if isinstance(data, dict)
        else {}
    )


def fetch_transaction(
    transaction_hash: str,
) -> dict[str, Any]:
    transaction_hash = (
        transaction_hash.lower()
    )

    if transaction_hash in FAILED_TX_HASHES:
        return {}

    if transaction_hash in TX_CACHE:
        return TX_CACHE[
            transaction_hash
        ]

    data = request_json(
        f"{PRO_REST_BASE_URL}/api/v2/transactions/"
        f"{transaction_hash}",
        description="Transaction",
    )

    if not isinstance(data, dict):
        FAILED_TX_HASHES.add(
            transaction_hash
        )
        return {}

    TX_CACHE[transaction_hash] = data

    return data


def fetch_internal_transactions(
    transaction_hash: str,
) -> list[dict[str, Any]]:
    transaction_hash = (
        transaction_hash.lower()
    )

    if (
        transaction_hash
        in FAILED_INTERNAL_HASHES
    ):
        return []

    if transaction_hash in INTERNAL_CACHE:
        return INTERNAL_CACHE[
            transaction_hash
        ]

    data = request_json(
        PRO_LEGACY_API_URL,
        params={
            "module": "account",
            "action": "txlistinternal",
            "txhash": transaction_hash,
        },
        description="Internal transaction",
    )

    rows = (
        data.get("result") or []
        if isinstance(data, dict)
        else []
    )

    if not isinstance(rows, list):
        FAILED_INTERNAL_HASHES.add(
            transaction_hash
        )
        return []

    INTERNAL_CACHE[
        transaction_hash
    ] = rows

    return rows


def fetch_address_info(
    address: str,
) -> dict[str, Any]:
    address = address.lower()

    if address in ADDRESS_CACHE:
        return ADDRESS_CACHE[address]

    data = request_json(
        f"{PRO_REST_BASE_URL}/api/v2/addresses/{address}",
        description="Address",
    )

    result = (
        data
        if isinstance(data, dict)
        else {}
    )

    ADDRESS_CACHE[address] = result

    return result


def fetch_token_transfers(
    contract: str,
    address: str | None = None,
    start_block: int = 0,
    max_pages: int = 5,
) -> list[dict[str, Any]] | None:
    collected = []
    seen_rows = set()

    for page in range(
        1,
        max_pages + 1,
    ):
        params = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract,
            "startblock": start_block,
            "endblock": 99_999_999,
            "page": page,
            "offset": PAGE_SIZE,
            "sort": "asc",
        }

        if address:
            params["address"] = address

        data = request_json(
            PRO_LEGACY_API_URL,
            params=params,
            description=(
                f"Token transfer page {page}"
            ),
        )

        if data is None:
            return None

        rows = (
            data.get("result") or []
            if isinstance(data, dict)
            else []
        )

        if not isinstance(rows, list):
            register_api_failure(
                f"Token transfer page {page}",
                "unexpected response format",
            )
            return None

        if not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue

            key = (
                str(
                    row.get("hash")
                    or row.get(
                        "transaction_hash"
                    )
                    or ""
                ).lower(),
                str(
                    row.get("logIndex")
                    or row.get("log_index")
                    or ""
                ),
                str(
                    row.get("from") or ""
                ).lower(),
                str(
                    row.get("to") or ""
                ).lower(),
                str(row.get("value") or ""),
            )

            if key in seen_rows:
                continue

            seen_rows.add(key)
            collected.append(row)

        if len(rows) < PAGE_SIZE:
            break

        time.sleep(REQUEST_DELAY)

    collected.sort(
        key=lambda row: (
            safe_int(
                row.get("blockNumber")
            ),
            safe_int(
                row.get(
                    "transactionIndex"
                )
            ),
            safe_int(
                row.get("logIndex")
                or row.get("log_index")
            ),
        )
    )

    return collected


# =========================================================
# TRANSACTION INTERPRETATION
# =========================================================

def transaction_succeeded(
    transaction: dict[str, Any],
) -> bool:
    return str(
        transaction.get("status") or ""
    ).lower() in {
        "ok",
        "success",
        "1",
    }


def transaction_method(
    transaction: dict[str, Any],
) -> str:
    method = str(
        transaction.get("method") or ""
    )

    if method:
        return method

    decoded = (
        transaction.get("decoded_input")
        or {}
    )

    method_call = str(
        decoded.get("method_call") or ""
    )

    if method_call:
        return method_call

    raw_input = str(
        transaction.get("raw_input")
        or transaction.get("input")
        or "0x"
    ).lower()

    return (
        raw_input[:10]
        if len(raw_input) >= 10
        else raw_input
    )


def is_swap(
    transaction: dict[str, Any],
) -> bool:
    method = transaction_method(
        transaction
    ).lower()

    if any(
        keyword in method
        for keyword in (
            "swap",
            "execute",
            "multicall",
            "exactinput",
            "exactoutput",
        )
    ):
        return True

    raw_input = str(
        transaction.get("raw_input")
        or transaction.get("input")
        or "0x"
    ).lower()

    selector = (
        raw_input[:10]
        if len(raw_input) >= 10
        else raw_input
    )

    return selector in SWAP_SELECTORS


def token_contract_from_transfer(
    transfer: dict[str, Any],
) -> str:
    token = transfer.get("token") or {}

    return str(
        token.get("address_hash")
        or token.get("address")
        or transfer.get("contractAddress")
        or ""
    ).lower()


def transfer_symbol(
    transfer: dict[str, Any],
) -> str:
    token = transfer.get("token") or {}

    return str(
        token.get("symbol")
        or transfer.get("tokenSymbol")
        or "UNKNOWN"
    ).upper()


def transfer_amount(
    transfer: dict[str, Any],
    fallback_decimals: int = 18,
) -> float:
    token = transfer.get("token") or {}
    total = transfer.get("total")

    raw_value = (
        total.get("value")
        if isinstance(total, dict)
        else (
            transfer.get("value")
            or transfer.get("amount")
            or 0
        )
    )

    decimals = safe_int(
        token.get("decimals")
        or transfer.get(
            "tokenDecimal"
        ),
        fallback_decimals,
    )

    try:
        return int(raw_value) / (
            10 ** decimals
        )

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return 0.0


def transaction_block(
    transaction: dict[str, Any],
) -> int:
    return safe_int(
        transaction.get("block")
        or transaction.get(
            "block_number"
        )
        or transaction.get(
            "blockNumber"
        )
    )


def transaction_time(
    transaction: dict[str, Any],
) -> str:
    return readable_time(
        transaction.get("timestamp")
        or transaction.get(
            "timeStamp"
        )
    )


def native_value_eth(
    transaction: dict[str, Any],
) -> float:
    value = transaction.get("value") or 0

    if isinstance(value, dict):
        value = (
            value.get("value")
            or value.get("raw")
            or 0
        )

    try:
        raw_value = (
            int(value, 16)
            if (
                isinstance(value, str)
                and value.startswith("0x")
            )
            else int(value or 0)
        )

        return raw_value / 10 ** 18

    except (
        TypeError,
        ValueError,
    ):
        return 0.0


def internal_eth_received(
    transaction_hash: str,
    wallet: str,
) -> float:
    total = 0.0

    for row in fetch_internal_transactions(
        transaction_hash
    ):
        recipient = str(
            row.get("to") or ""
        ).lower()

        is_error = (
            str(
                row.get("isError") or "0"
            )
            == "1"
        )

        if recipient != wallet:
            continue

        if is_error:
            continue

        try:
            total += (
                int(row.get("value") or 0)
                / 10 ** 18
            )

        except (
            TypeError,
            ValueError,
        ):
            continue

    return total


def quote_flows(
    transaction: dict[str, Any],
    transaction_hash: str,
    wallet: str,
    target_contract: str,
    include_internal_eth: bool,
) -> dict[str, dict[str, float]]:
    incoming: defaultdict[
        str,
        float,
    ] = defaultdict(float)

    outgoing: defaultdict[
        str,
        float,
    ] = defaultdict(float)

    if (
        normalise_address(
            transaction.get("from")
        )
        == wallet
    ):
        native_sent = native_value_eth(
            transaction
        )

        if native_sent > 0:
            outgoing["ETH"] += native_sent

    if include_internal_eth:
        native_received = (
            internal_eth_received(
                transaction_hash,
                wallet,
            )
        )

        if native_received > 0:
            incoming["ETH"] += (
                native_received
            )

    for transfer in (
        transaction.get(
            "token_transfers"
        )
        or []
    ):
        if (
            token_contract_from_transfer(
                transfer
            )
            == target_contract
        ):
            continue

        amount = transfer_amount(
            transfer
        )

        if amount <= 0:
            continue

        symbol = transfer_symbol(
            transfer
        )

        sender = normalise_address(
            transfer.get("from")
        )

        recipient = normalise_address(
            transfer.get("to")
        )

        if sender == wallet:
            outgoing[symbol] += amount

        if recipient == wallet:
            incoming[symbol] += amount

    return {
        "incoming": dict(incoming),
        "outgoing": dict(outgoing),
    }


def sum_symbols(
    flows: dict[str, float],
    symbols: set[str],
) -> float:
    return sum(
        safe_float(flows.get(symbol))
        for symbol in symbols
    )


def current_token_balance(
    contract: str,
    wallet: str,
    decimals: int,
) -> float:
    clean_wallet = (
        wallet.replace("0x", "")
        .rjust(64, "0")
    )

    result = rpc_call(
        "eth_call",
        [
            {
                "to": contract,
                "data": (
                    "0x70a08231"
                    + clean_wallet
                ),
            },
            "latest",
        ],
    )

    if not result or result == "0x":
        return 0.0

    try:
        return int(result, 16) / (
            10 ** decimals
        )

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return 0.0


def first_token_block(
    rows: list[dict[str, Any]],
) -> int | None:
    blocks = [
        safe_int(
            row.get("blockNumber")
        )
        for row in rows
        if safe_int(
            row.get("blockNumber")
        ) > 0
    ]

    return min(blocks) if blocks else None


# =========================================================
# EARLY BUYER DISCOVERY
# =========================================================

def find_early_buyers(
    rows: list[dict[str, Any]],
    contract: str,
    decimals: int,
) -> list[dict[str, Any]]:
    buyers = []
    seen_wallets = set()
    seen_transactions = set()

    for row in rows:
        transaction_hash = str(
            row.get("hash")
            or row.get(
                "transaction_hash"
            )
            or ""
        ).lower()

        if not transaction_hash:
            continue

        if (
            transaction_hash
            in seen_transactions
        ):
            continue

        seen_transactions.add(
            transaction_hash
        )

        transaction = fetch_transaction(
            transaction_hash
        )

        if (
            not transaction
            or not transaction_succeeded(
                transaction
            )
            or not is_swap(transaction)
        ):
            continue

        initiator = normalise_address(
            transaction.get("from")
        )

        recipient_amounts: defaultdict[
            str,
            float,
        ] = defaultdict(float)

        for transfer in (
            transaction.get(
                "token_transfers"
            )
            or []
        ):
            if (
                token_contract_from_transfer(
                    transfer
                )
                != contract
            ):
                continue

            recipient = normalise_address(
                transfer.get("to")
            )

            recipient_amounts[
                recipient
            ] += transfer_amount(
                transfer,
                decimals,
            )

        if (
            initiator
            and recipient_amounts.get(
                initiator,
                0.0,
            ) > 0
        ):
            candidate_wallets = [
                initiator
            ]
        else:
            candidate_wallets = list(
                recipient_amounts
            )

        for wallet in candidate_wallets:
            if (
                not wallet
                or wallet in ZERO_ADDRESSES
                or wallet in seen_wallets
            ):
                continue

            address_info = (
                fetch_address_info(wallet)
            )

            if address_info.get(
                "is_contract"
            ) is True:
                continue

            flows = quote_flows(
                transaction,
                transaction_hash,
                wallet,
                contract,
                include_internal_eth=False,
            )

            spent_eth = sum_symbols(
                flows["outgoing"],
                ETH_SYMBOLS,
            )

            spent_stable = sum_symbols(
                flows["outgoing"],
                STABLE_SYMBOLS,
            )

            if (
                spent_eth <= 0
                and spent_stable <= 0
            ):
                continue

            seen_wallets.add(wallet)

            buyer = {
                "wallet": wallet,
                "early_buy_rank": (
                    len(buyers) + 1
                ),
                "entry_transaction_hash": (
                    transaction_hash
                ),
                "entry_block": (
                    transaction_block(
                        transaction
                    )
                ),
                "entry_time_utc": (
                    transaction_time(
                        transaction
                    )
                ),
                "initial_tokens_received": (
                    recipient_amounts.get(
                        wallet,
                        0.0,
                    )
                ),
                "initial_spent_eth": (
                    spent_eth
                ),
                "initial_spent_stable": (
                    spent_stable
                ),
                "entry_method": (
                    transaction_method(
                        transaction
                    )
                ),
            }

            buyers.append(buyer)

            print(
                f"  Early buyer "
                f"#{len(buyers)}: {wallet}"
            )

            if (
                len(buyers)
                >= EARLY_BUYER_LIMIT
            ):
                return buyers

        time.sleep(REQUEST_DELAY)

    return buyers


# =========================================================
# RPC SALE PROBING
# =========================================================
#
# Hyperactive wallets can have hundreds of outbound target-token transfers.
# The old path fetched Blockscout /transactions/{hash} for every candidate.
# That is the source of the repeated HTTP 500 wall seen on 300+ candidate
# wallets.
#
# This path uses Robinhood Chain JSON-RPC transaction + receipt calls to reject
# ordinary transfers/non-swaps and identify quote-token proceeds. Blockscout
# internal transactions are requested only for the small subset of confirmed
# swaps that appear to pay the wallet in native ETH rather than WETH/stables.


TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa"
    "952ba7f163c4a11628f55a4df523b3ef"
)


def rpc_hex_to_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, str):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError):
        return default


def rpc_topic_to_address(topic: Any) -> str:
    value = str(topic or "").lower()

    if not value.startswith("0x") or len(value) < 42:
        return ""

    return normalise_address(
        "0x" + value[-40:]
    )


def rpc_decode_abi_string(raw_result: Any) -> str | None:
    if not raw_result or raw_result == "0x":
        return None

    raw_hex = str(raw_result)[2:]

    try:
        raw_bytes = bytes.fromhex(raw_hex)
    except ValueError:
        return None

    if len(raw_bytes) >= 64:
        try:
            offset = int.from_bytes(
                raw_bytes[0:32],
                byteorder="big",
            )

            if offset + 32 <= len(raw_bytes):
                length = int.from_bytes(
                    raw_bytes[offset:offset + 32],
                    byteorder="big",
                )

                start = offset + 32
                end = start + length

                if end <= len(raw_bytes):
                    return raw_bytes[start:end].decode(
                        "utf-8",
                        errors="replace",
                    )
        except (ValueError, UnicodeDecodeError):
            pass

    try:
        return raw_bytes.rstrip(b"\\x00").decode(
            "utf-8",
            errors="replace",
        )
    except UnicodeDecodeError:
        return None


def rpc_eth_call(contract: str, call_data: str) -> Any:
    return rpc_call(
        "eth_call",
        [
            {
                "to": contract,
                "data": call_data,
            },
            "latest",
        ],
    )


def rpc_token_metadata(contract: str) -> dict[str, Any]:
    contract = normalise_address(contract)

    if not contract:
        return {
            "symbol": "UNKNOWN",
            "decimals": 18,
        }

    if contract in RPC_TOKEN_METADATA_CACHE:
        return RPC_TOKEN_METADATA_CACHE[contract]

    symbol = rpc_decode_abi_string(
        rpc_eth_call(
            contract,
            "0x95d89b41",
        )
    ) or "UNKNOWN"

    decimals_result = rpc_eth_call(
        contract,
        "0x313ce567",
    )

    decimals = 18

    if decimals_result and decimals_result != "0x":
        try:
            decimals = int(
                str(decimals_result),
                16,
            )
        except ValueError:
            decimals = 18

    metadata = {
        "symbol": str(symbol).upper(),
        "decimals": decimals,
    }

    RPC_TOKEN_METADATA_CACHE[contract] = metadata
    return metadata


def rpc_block_timestamp(block_number: int) -> str | None:
    if block_number in RPC_BLOCK_TIMESTAMP_CACHE:
        return RPC_BLOCK_TIMESTAMP_CACHE[block_number]

    block = rpc_call(
        "eth_getBlockByNumber",
        [
            hex(block_number),
            False,
        ],
    )

    if not isinstance(block, dict):
        RPC_BLOCK_TIMESTAMP_CACHE[block_number] = None
        return None

    timestamp = rpc_hex_to_int(
        block.get("timestamp"),
        0,
    )

    if timestamp <= 0:
        RPC_BLOCK_TIMESTAMP_CACHE[block_number] = None
        return None

    result = datetime.fromtimestamp(
        timestamp,
        timezone.utc,
    ).isoformat()

    RPC_BLOCK_TIMESTAMP_CACHE[block_number] = result
    return result


def rpc_decode_transfer_log(
    log: dict[str, Any],
) -> dict[str, Any] | None:
    topics = log.get("topics") or []

    if len(topics) < 3:
        return None

    if str(topics[0]).lower() != TRANSFER_TOPIC.lower():
        return None

    contract = normalise_address(
        log.get("address")
    )
    sender = rpc_topic_to_address(
        topics[1]
    )
    recipient = rpc_topic_to_address(
        topics[2]
    )

    if not contract or not sender or not recipient:
        return None

    return {
        "contract": contract,
        "sender": sender,
        "recipient": recipient,
        "raw_amount": rpc_hex_to_int(
            log.get("data"),
            0,
        ),
    }


def rpc_probe_sale_candidate(
    transaction_hash: str,
    wallet: str,
    target_contract: str,
) -> dict[str, Any]:
    """
    Cheap sale classifier for one outbound token candidate.

    It does NOT use Blockscout's individual transaction endpoint. Successful
    results are persisted, so Ctrl+C/restarts do not repeat completed probes.
    """
    transaction_hash = str(
        transaction_hash or ""
    ).lower()
    wallet = wallet.lower()
    target_contract = target_contract.lower()

    cache_key = (
        f"{transaction_hash}|"
        f"{wallet}|"
        f"{target_contract}"
    )

    cached = SALE_PROBE_CACHE.get(
        cache_key
    )

    if isinstance(cached, dict):
        return cached

    transaction = rpc_call(
        "eth_getTransactionByHash",
        [transaction_hash],
    )

    if not isinstance(transaction, dict):
        return {
            "probe_complete": False,
            "reason": "RPC transaction unavailable",
        }

    receipt = rpc_call(
        "eth_getTransactionReceipt",
        [transaction_hash],
    )

    if not isinstance(receipt, dict):
        return {
            "probe_complete": False,
            "reason": "RPC receipt unavailable",
        }

    if rpc_hex_to_int(
        receipt.get("status"),
        0,
    ) != 1:
        result = {
            "probe_complete": True,
            "is_confirmed_sale": False,
            "reason": "Reverted transaction",
        }
        SALE_PROBE_CACHE[cache_key] = result
        return result

    # If the wallet did not initiate the transaction, this is normally a token
    # movement rather than the deliberate DEX exit we are trying to measure.
    if normalise_address(
        transaction.get("from")
    ) != wallet:
        result = {
            "probe_complete": True,
            "is_confirmed_sale": False,
            "reason": "Wallet was not transaction initiator",
        }
        SALE_PROBE_CACHE[cache_key] = result
        return result

    raw_input = str(
        transaction.get("input")
        or "0x"
    ).lower()

    selector = (
        raw_input[:10]
        if len(raw_input) >= 10
        else raw_input
    )

    if selector not in SWAP_SELECTORS:
        result = {
            "probe_complete": True,
            "is_confirmed_sale": False,
            "reason": "Non-swap selector",
            "selector": selector,
        }
        SALE_PROBE_CACHE[cache_key] = result
        return result

    target_out_seen = False

    incoming_quote: defaultdict[
        str,
        float,
    ] = defaultdict(float)

    for log in receipt.get("logs") or []:
        transfer = rpc_decode_transfer_log(
            log
        )

        if transfer is None:
            continue

        if (
            transfer["contract"] == target_contract
            and transfer["sender"] == wallet
        ):
            target_out_seen = True
            continue

        if transfer["recipient"] != wallet:
            continue

        if transfer["contract"] == target_contract:
            continue

        metadata = rpc_token_metadata(
            transfer["contract"]
        )

        decimals = safe_int(
            metadata.get("decimals"),
            18,
        )

        try:
            amount = (
                transfer["raw_amount"]
                / (10 ** decimals)
            )
        except (
            TypeError,
            ValueError,
            OverflowError,
        ):
            amount = 0.0

        if amount <= 0:
            continue

        symbol = str(
            metadata.get("symbol")
            or "UNKNOWN"
        ).upper()

        incoming_quote[symbol] += amount

    if not target_out_seen:
        result = {
            "probe_complete": True,
            "is_confirmed_sale": False,
            "reason": "No target-token transfer out in receipt",
            "selector": selector,
        }
        SALE_PROBE_CACHE[cache_key] = result
        return result

    received_eth = sum(
        safe_float(
            incoming_quote.get(symbol)
        )
        for symbol in ETH_SYMBOLS
    )

    received_stable = sum(
        safe_float(
            incoming_quote.get(symbol)
        )
        for symbol in STABLE_SYMBOLS
    )

    used_internal_fallback = False

    # Native ETH has no ERC-20 Transfer log. Only after RPC has proved this is
    # a genuine swap do we use the Blockscout internal endpoint as a fallback.
    if (
        received_eth <= 0
        and received_stable <= 0
    ):
        failures_before = (
            API_FAILURE_COUNT
        )

        native_eth = internal_eth_received(
            transaction_hash,
            wallet,
        )

        if native_eth > 0:
            received_eth = native_eth
            used_internal_fallback = True

        elif API_FAILURE_COUNT > failures_before:
            return {
                "probe_complete": False,
                "reason": (
                    "Confirmed swap but native-ETH "
                    "proceeds lookup failed"
                ),
            }

    if (
        received_eth <= 0
        and received_stable <= 0
    ):
        result = {
            "probe_complete": True,
            "is_confirmed_sale": False,
            "reason": "No recognised ETH/WETH/stable proceeds",
            "selector": selector,
        }
        SALE_PROBE_CACHE[cache_key] = result
        return result

    block_number = rpc_hex_to_int(
        receipt.get("blockNumber")
        or transaction.get("blockNumber"),
        0,
    )

    quote_flows = {
        "incoming": dict(
            incoming_quote
        ),
        "outgoing": {},
    }

    if (
        used_internal_fallback
        and received_eth > 0
    ):
        quote_flows[
            "incoming"
        ]["ETH"] = received_eth

    result = {
        "probe_complete": True,
        "is_confirmed_sale": True,
        "selector": selector,
        "method": selector,
        "timestamp_utc": (
            rpc_block_timestamp(
                block_number
            )
            if block_number > 0
            else None
        ),
        "received_eth": received_eth,
        "received_stable": received_stable,
        "quote_flows": quote_flows,
        "used_internal_eth_fallback": (
            used_internal_fallback
        ),
    }

    SALE_PROBE_CACHE[cache_key] = result

    # Persist frequently during noisy wallets so a stop/restart does not redo
    # hundreds of already-classified candidates.
    if len(SALE_PROBE_CACHE) % 25 == 0:
        save_persistent_api_caches()

    return result


# =========================================================
# FIRST EXIT ANALYSIS
# =========================================================

def row_amount(
    row: dict[str, Any],
    decimals: int,
) -> float:
    try:
        return safe_int(
            row.get("value")
        ) / (
            10
            ** safe_int(
                row.get("tokenDecimal"),
                decimals,
            )
        )

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return 0.0


def outgoing_transactions_after_entry(
    rows: list[dict[str, Any]],
    wallet: str,
    entry_block: int,
    entry_transaction_hash: str,
    decimals: int,
) -> list[dict[str, Any]]:
    """
    Ignore all incoming transfers and group only outbound token
    transfers occurring after the known first-buy transaction.
    """

    grouped: dict[
        str,
        dict[str, Any],
    ] = {}

    for row in rows:
        sender = str(
            row.get("from") or ""
        ).lower()

        if sender != wallet:
            continue

        block_number = safe_int(
            row.get("blockNumber")
        )

        transaction_hash = str(
            row.get("hash")
            or row.get(
                "transaction_hash"
            )
            or ""
        ).lower()

        if not transaction_hash:
            continue

        if (
            block_number < entry_block
            or transaction_hash
            == entry_transaction_hash
        ):
            continue

        if transaction_hash not in grouped:
            grouped[transaction_hash] = {
                "transaction_hash": (
                    transaction_hash
                ),
                "block_number": (
                    block_number
                ),
                "transaction_index": (
                    safe_int(
                        row.get(
                            "transactionIndex"
                        )
                    )
                ),
                "tokens_out": 0.0,
            }

        grouped[
            transaction_hash
        ]["tokens_out"] += row_amount(
            row,
            decimals,
        )

    return sorted(
        grouped.values(),
        key=lambda item: (
            item["block_number"],
            item["transaction_index"],
        ),
    )


def calculate_trader_score(
    realised_roi: float | None,
    realised_profit: float | None,
    seconds_after_launch: int | None,
    time_to_exit_seconds: int | None,
    exact_profit: bool,
) -> float:
    score = 0.0

    if realised_roi is not None:
        if realised_roi >= 2_000:
            score += 50
        elif realised_roi >= 1_000:
            score += 45
        elif realised_roi >= 500:
            score += 38
        elif realised_roi >= 200:
            score += 30
        elif realised_roi >= 100:
            score += 22
        elif realised_roi > 0:
            score += 12

    if realised_profit is not None:
        if realised_profit >= 5:
            score += 20
        elif realised_profit >= 2:
            score += 17
        elif realised_profit >= 1:
            score += 14
        elif realised_profit >= 0.25:
            score += 10
        elif realised_profit > 0:
            score += 5

    if seconds_after_launch is not None:
        if seconds_after_launch <= 30:
            score += 20
        elif seconds_after_launch <= 120:
            score += 17
        elif seconds_after_launch <= 600:
            score += 13
        elif seconds_after_launch <= 3_600:
            score += 9
        elif seconds_after_launch <= 21_600:
            score += 5

    if time_to_exit_seconds is not None:
        # A confirmed, deliberate exit receives a small bonus.
        score += 5

    if not exact_profit:
        score = min(score, 35)

    return max(
        0.0,
        min(score, 100.0),
    )


def analyse_first_entry_first_exit(
    buyer: dict[str, Any],
    contract: str,
    decimals: int,
    launch_time: datetime | None,
    launch_block: int,
) -> dict[str, Any]:
    """
    Analyse the known first early buy against cumulative confirmed sells.

    Later incoming buys are ignored. Only outbound target-token transfers
    are candidates. Those candidates are classified with Robinhood Chain RPC
    transaction receipts rather than a Blockscout transaction-detail request
    for every candidate. Confirmed proceeds are accumulated until approximately
    the original early-buy allocation has been sold, or no candidates remain.
    """

    trader_api_failure_start = API_FAILURE_COUNT

    wallet = buyer["wallet"]
    entry_hash = buyer["entry_transaction_hash"]
    entry_block = safe_int(buyer.get("entry_block"))
    entry_time_text = buyer.get("entry_time_utc")
    entry_time = parse_time(entry_time_text)

    initial_tokens = safe_float(
        buyer.get("initial_tokens_received")
    )
    initial_spent_eth = safe_float(
        buyer.get("initial_spent_eth")
    )
    initial_spent_stable = safe_float(
        buyer.get("initial_spent_stable")
    )

    wallet_rows = fetch_token_transfers(
        contract,
        address=wallet,
        start_block=entry_block,
        max_pages=MAX_WALLET_PAGES,
    )

    if wallet_rows is None:
        wallet_rows = []

    outgoing_candidates = outgoing_transactions_after_entry(
        rows=wallet_rows,
        wallet=wallet,
        entry_block=entry_block,
        entry_transaction_hash=entry_hash,
        decimals=decimals,
    )

    print(
        f"      Outgoing candidates: "
        f"{len(outgoing_candidates)}"
    )

    confirmed_sells = []
    failed_transactions = []

    cumulative_tokens_sold = 0.0
    cumulative_received_eth = 0.0
    cumulative_received_stable = 0.0
    candidates_checked = 0

    target_tokens_to_exit = (
        initial_tokens
        * ORIGINAL_POSITION_EXIT_FRACTION
    )

    for candidate_index, candidate in enumerate(
        outgoing_candidates,
        start=1,
    ):
        candidates_checked = candidate_index
        transaction_hash = candidate[
            "transaction_hash"
        ]

        print(
            f"      RPC sale probe "
            f"{candidate_index}/"
            f"{len(outgoing_candidates)}",
            end="\r",
            flush=True,
        )

        sale_probe = rpc_probe_sale_candidate(
            transaction_hash=transaction_hash,
            wallet=wallet,
            target_contract=contract,
        )

        if not sale_probe.get(
            "probe_complete"
        ):
            failed_transactions.append(
                transaction_hash
            )

            print(
                f"\n      Sale probe unavailable for "
                f"{transaction_hash}: "
                f"{sale_probe.get('reason', 'unknown reason')}. "
                "Stopping only this trader; completed probes are cached."
            )
            break

        if not sale_probe.get(
            "is_confirmed_sale"
        ):
            continue

        received_eth = safe_float(
            sale_probe.get("received_eth")
        )

        received_stable = safe_float(
            sale_probe.get(
                "received_stable"
            )
        )

        tokens_out = safe_float(
            candidate.get("tokens_out")
        )

        original_tokens_remaining = max(
            initial_tokens
            - cumulative_tokens_sold,
            0.0,
        )

        counted_tokens = min(
            tokens_out,
            original_tokens_remaining,
        )

        if counted_tokens <= 0:
            break

        proceeds_fraction = (
            counted_tokens / tokens_out
            if tokens_out > 0
            else 0.0
        )

        counted_received_eth = (
            received_eth
            * proceeds_fraction
        )

        counted_received_stable = (
            received_stable
            * proceeds_fraction
        )

        sell_event = {
            **candidate,
            "timestamp_utc": sale_probe.get(
                "timestamp_utc"
            ),
            "method": sale_probe.get(
                "method"
            ),
            "tokens_out_counted": (
                counted_tokens
            ),
            "received_eth": (
                counted_received_eth
            ),
            "received_stable": (
                counted_received_stable
            ),
            "quote_flows": sale_probe.get(
                "quote_flows"
            ) or {
                "incoming": {},
                "outgoing": {},
            },
            "classification_source": (
                "Robinhood RPC receipt"
            ),
            "used_internal_eth_fallback": (
                sale_probe.get(
                    "used_internal_eth_fallback",
                    False,
                )
            ),
        }

        confirmed_sells.append(
            sell_event
        )

        cumulative_tokens_sold += (
            counted_tokens
        )

        cumulative_received_eth += (
            counted_received_eth
        )

        cumulative_received_stable += (
            counted_received_stable
        )

        # Same original rule: once about 95% of the original early-buy
        # allocation has been accounted for, later wallet activity is irrelevant.
        if (
            initial_tokens > 0
            and cumulative_tokens_sold
            >= target_tokens_to_exit
        ):
            break

    if outgoing_candidates:
        print(
            " " * 70,
            end="\r",
        )

    current_balance = current_token_balance(
        contract,
        wallet,
        decimals,
    )

    seconds_after_launch = (
        int(
            (
                entry_time
                - launch_time
            ).total_seconds()
        )
        if (
            entry_time is not None
            and launch_time is not None
        )
        else None
    )

    blocks_after_launch = (
        entry_block - launch_block
        if entry_block and launch_block
        else None
    )

    sold_fraction = (
        min(
            cumulative_tokens_sold
            / initial_tokens,
            1.0,
        )
        if initial_tokens > 0
        else 0.0
    )

    percentage_sold = (
        sold_fraction * 100
        if initial_tokens > 0
        else None
    )

    percentage_remaining = (
        current_balance
        / initial_tokens
        * 100
        if initial_tokens > 0
        else None
    )

    cost_basis_eth = (
        initial_spent_eth
        * sold_fraction
    )
    cost_basis_stable = (
        initial_spent_stable
        * sold_fraction
    )

    realised_profit = None
    realised_roi = None
    ranking_asset = None

    if not confirmed_sells:
        profit_status = (
            "No confirmed cumulative sale found"
        )

    elif (
        cost_basis_eth > 0
        and initial_spent_stable == 0
        and cumulative_received_stable == 0
    ):
        realised_profit = (
            cumulative_received_eth
            - cost_basis_eth
        )
        realised_roi = (
            realised_profit
            / cost_basis_eth
            * 100
        )
        ranking_asset = "ETH"
        profit_status = (
            "Cumulative ETH/WETH realised estimate"
        )

    elif (
        cost_basis_stable > 0
        and initial_spent_eth == 0
        and cumulative_received_eth == 0
    ):
        realised_profit = (
            cumulative_received_stable
            - cost_basis_stable
        )
        realised_roi = (
            realised_profit
            / cost_basis_stable
            * 100
        )
        ranking_asset = "USD stable"
        profit_status = (
            "Cumulative stablecoin realised estimate"
        )

    else:
        profit_status = (
            "Mixed/unresolved cumulative quote assets"
        )

    first_exit = (
        confirmed_sells[0]
        if confirmed_sells
        else None
    )
    final_counted_exit = (
        confirmed_sells[-1]
        if confirmed_sells
        else None
    )

    first_exit_time = (
        parse_time(
            first_exit.get("timestamp_utc")
        )
        if first_exit
        else None
    )
    final_exit_time = (
        parse_time(
            final_counted_exit.get(
                "timestamp_utc"
            )
        )
        if final_counted_exit
        else None
    )

    time_to_first_exit_seconds = (
        int(
            (
                first_exit_time
                - entry_time
            ).total_seconds()
        )
        if (
            first_exit_time is not None
            and entry_time is not None
        )
        else None
    )

    time_to_final_exit_seconds = (
        int(
            (
                final_exit_time
                - entry_time
            ).total_seconds()
        )
        if (
            final_exit_time is not None
            and entry_time is not None
        )
        else None
    )

    exact_profit = realised_roi is not None

    score = calculate_trader_score(
        realised_roi=realised_roi,
        realised_profit=realised_profit,
        seconds_after_launch=seconds_after_launch,
        time_to_exit_seconds=time_to_final_exit_seconds,
        exact_profit=exact_profit,
    )

    qualifies_profit_filter = (
        ranking_asset == "ETH"
        and realised_profit is not None
        and realised_profit
        >= MIN_REALISED_PROFIT_ETH
    )

    return {
        "wallet": wallet,
        "early_buy_rank": buyer["early_buy_rank"],
        "entry_transaction_hash": entry_hash,
        "entry_time_utc": entry_time_text,
        "entry_block": entry_block,
        "seconds_after_launch": seconds_after_launch,
        "delay_after_launch": format_duration(
            seconds_after_launch
        ),
        "blocks_after_launch": blocks_after_launch,
        "initial_tokens_bought": initial_tokens,
        "initial_spent_eth": initial_spent_eth,
        "initial_spent_stable": initial_spent_stable,
        "confirmed_sell_count": len(confirmed_sells),
        "confirmed_sells": confirmed_sells,
        "cumulative_tokens_sold": cumulative_tokens_sold,
        "cumulative_percentage_sold": percentage_sold,
        "cumulative_received_eth": cumulative_received_eth,
        "cumulative_received_stable": cumulative_received_stable,
        "cumulative_cost_basis_eth": cost_basis_eth,
        "cumulative_cost_basis_stable": cost_basis_stable,
        "first_exit_time_utc": (
            first_exit.get("timestamp_utc")
            if first_exit
            else None
        ),
        "final_counted_exit_time_utc": (
            final_counted_exit.get("timestamp_utc")
            if final_counted_exit
            else None
        ),
        "time_to_first_exit": format_duration(
            time_to_first_exit_seconds
        ),
        "time_to_final_counted_exit": format_duration(
            time_to_final_exit_seconds
        ),
        "current_balance": current_balance,
        "percentage_of_initial_buy_remaining": percentage_remaining,
        "realised_profit": realised_profit,
        "realised_roi": realised_roi,
        "ranking_asset": ranking_asset,
        "profit_status": profit_status,
        "qualifies_profit_filter": qualifies_profit_filter,
        "minimum_profit_required_eth": MIN_REALISED_PROFIT_ETH,
        "trader_score": score,
        "outgoing_candidates_checked": candidates_checked,
        "failed_transaction_hashes": failed_transactions,
        "analysis_complete": (
            not failed_transactions
            and API_FAILURE_COUNT == trader_api_failure_start
        ),
        "api_failures_during_analysis": (
            API_FAILURE_COUNT - trader_api_failure_start
        ),
    }


def rank_traders(
    traders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank qualifying wallets by absolute ETH profit first."""

    ranked = sorted(
        traders,
        key=lambda trader: (
            trader.get("qualifies_profit_filter") is True,
            safe_float(
                trader.get("realised_profit"),
                -(10 ** 18),
            ),
            safe_float(
                trader.get("realised_roi"),
                -(10 ** 18),
            ),
            -safe_int(
                trader.get("seconds_after_launch"),
                10 ** 18,
            ),
            safe_float(trader.get("trader_score")),
        ),
        reverse=True,
    )

    for rank, trader in enumerate(ranked, start=1):
        trader["performance_rank"] = rank

    return ranked


# =========================================================
# SAVING
# =========================================================

def load_existing_top_results() -> list[dict[str, Any]]:
    """
    Load the cumulative smart-wallet/token database.

    top_early_traders.json is deliberately treated as an independent,
    cumulative history. It must never be rebuilt solely from the smaller
    current early_trader_results.json file.
    """
    data = read_json(
        TOP_OUTPUT_FILE,
        {},
    )

    if not isinstance(data, dict):
        return []

    results = data.get("results") or []

    return [
        result
        for result in results
        if isinstance(result, dict)
        and report_contract(result)
    ]


def merge_top_results(
    existing_results: list[dict[str, Any]],
    completed_reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Preserve every historical token already in top_early_traders.json and
    update only token contracts that have a newly completed analysis.

    This prevents a newer/reduced early_trader_results.json from deleting
    previously discovered smart wallets.
    """
    by_contract: dict[str, dict[str, Any]] = {}

    for result in existing_results:
        contract = report_contract(result)

        if contract:
            by_contract[contract] = result

    for report in completed_reports:
        contract = report_contract(report)

        if not contract:
            continue

        # save_all_reports is called only with completed reports. Therefore
        # a completed reanalysis is allowed to replace that token's previous
        # top-trader result, including a genuine zero-qualifier result.
        by_contract[contract] = {
            "token": report["token"],
            "launch": report["launch"],
            "top_traders": report.get("top_traders") or [],
        }

    return list(by_contract.values())


def save_checkpoint(
    token: dict[str, Any],
    buyers: list[dict[str, Any]],
    traders: list[dict[str, Any]],
) -> None:
    checkpoint_data = {
            "updated_at_utc": utc_now(),
            "token": token,
            "early_buyers_found": len(
                buyers
            ),
            "traders_completed": len(
                traders
            ),
            "trader_results": traders,
            "failed_transaction_hashes": (
                sorted(
                    FAILED_TX_HASHES
                )
            ),
            "api_failure_count": API_FAILURE_COUNT,
            "api_failure_log": API_FAILURE_LOG[-100:],
        }

    # Keep the legacy latest-checkpoint file for readability, but also keep a
    # separate checkpoint per token so moving on to Arrow can never overwrite
    # CashCat's completed trader work.
    write_json(CHECKPOINT_FILE, checkpoint_data)
    contract = str((token or {}).get("contract") or "").lower()
    if contract:
        write_json(token_checkpoint_path(contract), checkpoint_data)

    # Successful transaction/internal responses are flushed after every trader.
    save_persistent_api_caches()


def save_all_reports(
    reports: list[dict[str, Any]],
    scanner_token_count: int,
) -> None:
    combined_reports = merge_reports(
        reports
    )

    # IMPORTANT:
    # The full analysis file may contain only the reports currently available
    # to this analyser. The top-wallet file is a separate cumulative database.
    # Load it first and merge newly completed token results into it so older
    # profitable wallets can never disappear simply because their old full
    # report is no longer present.
    existing_top_results = load_existing_top_results()

    cumulative_top_results = merge_top_results(
        existing_results=existing_top_results,
        completed_reports=combined_reports,
    )

    write_json(
        FULL_OUTPUT_FILE,
        {
            "generated_at_utc": utc_now(),
            "scanner_tokens_available": (
                scanner_token_count
            ),
            "tokens_completed": len(
                combined_reports
            ),
            "reports": combined_reports,
            "failed_transaction_hashes": (
                sorted(
                    FAILED_TX_HASHES
                )
            ),
            "failed_internal_hashes": (
                sorted(
                    FAILED_INTERNAL_HASHES
                )
            ),
        },
    )

    backup_top_wallet_database()

    write_json(
        TOP_OUTPUT_FILE,
        {
            "generated_at_utc": utc_now(),
            "tokens_completed": len(
                cumulative_top_results
            ),
            "results": cumulative_top_results,
        },
    )


# =========================================================
# TOKEN ANALYSIS
# =========================================================

def analyse_token(
    scanner_token: dict[str, Any],
    run_index: int,
    run_total: int,
) -> dict[str, Any] | None:
    token_api_failure_start = API_FAILURE_COUNT

    contract = scanner_token[
        "contract"
    ]

    token_info = fetch_token_info(
        contract
    )

    name = (
        token_info.get("name")
        or scanner_token.get("name")
        or "Unknown"
    )

    symbol = (
        token_info.get("symbol")
        or scanner_token.get("symbol")
        or "UNKNOWN"
    )

    decimals = safe_int(
        token_info.get("decimals"),
        18,
    )

    print("\n" + "=" * 100)
    print(
        f"[{run_index}/{run_total}] "
        f"{name} ({symbol})"
    )
    print("=" * 100)

    token_rows = fetch_token_transfers(
        contract,
        max_pages=MAX_TOKEN_PAGES,
    )

    if token_rows is None:
        print(
            "Token transfer history could not be fetched. "
            "Leaving this token pending for the next run."
        )
        return None

    if not token_rows:
        print(
            "No token transfer history was returned. "
            "Leaving this token pending for the next run."
        )
        return None

    start_block = first_token_block(
        token_rows
    )

    if start_block is None:
        print(
            "Could not find the token's "
            "first block. Skipping."
        )
        return None

    early_buyers = find_early_buyers(
        token_rows,
        contract,
        decimals,
    )

    if not early_buyers:
        print(
            "No genuine early DEX buyers "
            "were found. Skipping."
        )
        return None

    launch_time = parse_time(
        early_buyers[0][
            "entry_time_utc"
        ]
    )

    launch_block = safe_int(
        early_buyers[0][
            "entry_block"
        ]
    )

    print(
        f"\n  Launch reference: "
        f"{early_buyers[0]['entry_time_utc']} | "
        f"block {launch_block:,}"
    )

    traders = []

    token_summary = {
        "name": name,
        "symbol": symbol,
        "contract": contract,
        "decimals": decimals,
    }

    # Resume this token trader-by-trader. Only fully completed trader results
    # are reused; incomplete traders are retried.
    token_checkpoint = load_token_checkpoint(contract)
    checkpoint_results = token_checkpoint.get("trader_results") or []
    completed_by_wallet = {
        str(item.get("wallet") or "").lower(): item
        for item in checkpoint_results
        if isinstance(item, dict) and item.get("analysis_complete") is True
    }

    if completed_by_wallet:
        print(f"  Resuming checkpoint: {len(completed_by_wallet)} completed trader(s) will be reused without API calls.")

    for trader_index, buyer in enumerate(
        early_buyers,
        start=1,
    ):
        buyer_wallet = str(buyer.get("wallet") or "").lower()
        if buyer_wallet in completed_by_wallet:
            print(
                f"\n  Reusing completed trader {trader_index}/{len(early_buyers)}: "
                f"{buyer_wallet}"
            )
            traders.append(completed_by_wallet[buyer_wallet])
            continue

        print(
            f"\n  Analysing trader "
            f"{trader_index}/"
            f"{len(early_buyers)}: "
            f"{buyer['wallet']}"
        )

        result = (
            analyse_first_entry_first_exit(
                buyer=buyer,
                contract=contract,
                decimals=decimals,
                launch_time=launch_time,
                launch_block=launch_block,
            )
        )

        traders.append(result)

        save_checkpoint(
            token=token_summary,
            buyers=early_buyers,
            traders=traders,
        )

    incomplete_traders = [
        trader
        for trader in traders
        if trader.get("analysis_complete") is not True
    ]

    token_api_failures = (
        API_FAILURE_COUNT - token_api_failure_start
    )

    if incomplete_traders or token_api_failures:
        print(
            "\n  Token analysis incomplete: "
            f"{len(incomplete_traders)} trader(s) incomplete; "
            f"{token_api_failures} API failure(s)."
        )
        print(
            "  Results were checkpointed, but this token will NOT be "
            "marked completed. It will be retried on the next run."
        )
        return None

    ranked = rank_traders(
        traders
    )

    qualifying_traders = [
        trader
        for trader in ranked
        if trader.get(
            "qualifies_profit_filter"
        ) is True
    ]

    top_traders = qualifying_traders[
        :TOP_TRADERS_PER_TOKEN
    ]

    print("\n  TOP FIVE EARLY TRADERS")
    print("  " + "-" * 90)

    for trader in top_traders:
        line = (
            f"  #{trader['performance_rank']} "
            f"{trader['wallet']} | "
            f"entry "
            f"{trader['delay_after_launch']} | "
            f"final exit "
            f"{trader['time_to_final_counted_exit']} | "
            f"score "
            f"{trader['trader_score']:.1f}/100"
        )

        if (
            trader["realised_roi"]
            is not None
        ):
            line += (
                f" | ROI "
                f"{trader['realised_roi']:.2f}%"
                f" | profit "
                f"{format_number(trader['realised_profit'])} "
                f"{trader['ranking_asset']}"
            )
        else:
            line += (
                f" | "
                f"{trader['profit_status']}"
            )

        if not trader[
            "analysis_complete"
        ]:
            line += (
                " | INCOMPLETE API DATA"
            )

        print(line)

    return {
        "token": {
            **token_summary,
            "scanner_data": scanner_token,
        },
        "launch": {
            "first_token_activity_block": (
                start_block
            ),
            "first_confirmed_buy_block": (
                launch_block
            ),
            "first_confirmed_buy_time_utc": (
                early_buyers[0][
                    "entry_time_utc"
                ]
            ),
        },
        "token_analysis_complete": True,
        "api_failures_during_token": 0,
        "analysis_method": (
            "Known first entry to cumulative "
            "confirmed sells"
        ),
        "settings": {
            "early_buyer_limit": (
                EARLY_BUYER_LIMIT
            ),
            "top_traders_per_token": (
                TOP_TRADERS_PER_TOKEN
            ),
            "later_incoming_buys_ignored": (
                True
            ),
            "minimum_realised_profit_eth": (
                MIN_REALISED_PROFIT_ETH
            ),
            "original_position_exit_fraction": (
                ORIGINAL_POSITION_EXIT_FRACTION
            ),
        },
        "early_buyers": early_buyers,
        "ranked_traders": ranked,
        "top_traders": top_traders,
    }


# =========================================================
# MAIN
# =========================================================

def main() -> None:
    if not BLOCKSCOUT_API_KEY:
        print(
            "BLOCKSCOUT_API_KEY is not loaded. Add it as a GitHub "
            "Codespaces secret, restart the Codespace, and try again."
        )
        return

    load_persistent_api_caches()

    print(
        "Blockscout PRO API authenticated for "
        f"Robinhood Chain ID {CHAIN_ID}."
    )

    scanner_tokens = (
        load_scanner_tokens()
    )

    if not scanner_tokens:
        print(
            f"No tokens were loaded from "
            f"{INPUT_FILE}."
        )
        return

    existing_reports = (
        load_existing_reports()
    )

    forced_recheck_contracts = {
        report_contract(report)
        for report in existing_reports
        if str(
            (report.get("token") or {}).get("symbol") or ""
        ).upper() in FORCE_RECHECK_TOKEN_SYMBOLS
    }

    if forced_recheck_contracts:
        print(
            "Removing previously saved report(s) that must be "
            "rechecked through the PRO API: "
            f"{len(forced_recheck_contracts)}"
        )
        existing_reports = [
            report
            for report in existing_reports
            if report_contract(report) not in forced_recheck_contracts
        ]

    incomplete_existing_contracts = {
        report_contract(report)
        for report in existing_reports
        if not report_is_complete(report)
    }

    if incomplete_existing_contracts:
        print(
            "Removing incomplete saved token reports so they can be "
            f"retried: {len(incomplete_existing_contracts)}"
        )
        existing_reports = [
            report
            for report in existing_reports
            if report_contract(report)
            not in incomplete_existing_contracts
        ]

    completed_contracts = {
        report_contract(report)
        for report in existing_reports
        if report_is_complete(report)
        and report.get("analysis_method")
        != "Known first entry to first confirmed sale"
    }

    stale_first_exit_contracts = {
        report_contract(report)
        for report in existing_reports
        if report.get("analysis_method")
        == "Known first entry to first confirmed sale"
    }

    if stale_first_exit_contracts:
        existing_reports = [
            report
            for report in existing_reports
            if report_contract(report)
            not in stale_first_exit_contracts
        ]

        print(
            "Reprocessing tokens previously analysed "
            "with first-exit-only logic: "
            f"{len(stale_first_exit_contracts)}"
        )

    tokens_to_process = pending_tokens(
        scanner_tokens,
        completed_contracts,
    )

    # Skip only explicitly excluded symbols. This currently removes VEX from
    # the run without changing retry behaviour or analysis for any other token.
    explicitly_skipped_tokens = [
        token
        for token in tokens_to_process
        if str(token.get("symbol") or "").upper()
        in SKIP_TOKEN_SYMBOLS
    ]

    tokens_to_process = [
        token
        for token in tokens_to_process
        if (
            str(token.get("symbol") or "").upper()
            not in SKIP_TOKEN_SYMBOLS
            and normalise_address(token.get("contract"))
            not in SKIP_TOKEN_CONTRACTS
        )
    ]

    if explicitly_skipped_tokens:
        print("Explicitly excluded from this analyser:")
        for token in explicitly_skipped_tokens:
            print(
                f"  - {token.get('name') or 'Unknown'} "
                f"({token.get('symbol') or 'UNKNOWN'})"
            )

    print(
        f"Stage 1 + recovery tokens available: "
        f"{len(scanner_tokens)}"
    )

    print(
        f"Previously completed tokens: "
        f"{len(completed_contracts)}"
    )

    if completed_contracts:
        print(
            "Already completed and skipped:"
        )

        for report in existing_reports:
            token = report.get(
                "token"
            ) or {}

            print(
                f"  - "
                f"{token.get('name') or 'Unknown'} "
                f"({token.get('symbol') or 'UNKNOWN'}) "
                f"{token.get('contract')}"
            )

    if not tokens_to_process:
        print(
            "\nNo new tokens remain to analyse."
        )

        save_all_reports(
            existing_reports,
            len(scanner_tokens),
        )
        return

    print(
        f"New tokens to analyse: "
        f"{len(tokens_to_process)}"
    )

    all_reports = list(
        existing_reports
    )

    for index, token in enumerate(
        tokens_to_process,
        start=1,
    ):
        report = analyse_token(
            scanner_token=token,
            run_index=index,
            run_total=len(
                tokens_to_process
            ),
        )

        if report is not None:
            all_reports.append(report)

            # Save immediately after each completed token.
            save_all_reports(
                all_reports,
                len(scanner_tokens),
            )

        time.sleep(TOKEN_DELAY)

    print("\n" + "=" * 100)
    print("STAGE 2 COMPLETE")
    print("=" * 100)

    print(
        f"Combined completed tokens: "
        f"{len(merge_reports(all_reports))}"
    )

    print(
        f"Full results saved to: "
        f"{FULL_OUTPUT_FILE}"
    )

    print(
        f"Top five per token saved to: "
        f"{TOP_OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()