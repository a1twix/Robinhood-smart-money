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
# CONVICTION HOLDER ANALYSER
# =========================================================
#
# Purpose:
#   A separate smart-wallet discovery engine that runs alongside
#   early_buyer_analyzer.py without changing it.
#
# Reads:
#   scanner_qualifying_tokens.json
#
# For each token:
#   1. Fetch the CURRENT Top 20 holders.
#   2. Ignore contract addresses / zero addresses.
#   3. Find each holder's FIRST genuine DEX buy of that token.
#   4. Keep only holders whose first genuine buy occurred within
#      30 minutes of the token's first confirmed genuine DEX buy.
#   5. Sort qualifying current holders by earliest entry.
#   6. Save ALL qualifying holders to the full results file.
#   7. Save the earliest 5 qualifying holders per token to the
#      live smart-wallet file.
#
# Also records, for every qualifying holder:
#   - current holder rank
#   - first-buy time after launch
#   - initial tokens bought
#   - initial ETH / stablecoin spent
#   - confirmed token sales
#   - percentage of original early buy sold
#   - current token balance
#   - current balance as % of original early buy
#   - realised ETH / stable proceeds
#   - realised profit / ROI where it can be calculated safely
#
# Saves:
#   conviction_holder_results.json
#   top_conviction_holders.json
#   conviction_holder_checkpoint.json
#
# IMPORTANT PERSISTENCE RULE:
#   top_conviction_holders.json is CUMULATIVE.
#   A later run never deletes unrelated historical token results.
# =========================================================


# =========================================================
# SETTINGS
# =========================================================

CHAIN_ID = 4663

BLOCKSCOUT_API_KEY = os.getenv("BLOCKSCOUT_API_KEY", "").strip()

# Matches the PRO API setup already used by early_buyer_analyzer.py.
PRO_REST_BASE_URL = f"https://api.blockscout.com/{CHAIN_ID}"
PRO_LEGACY_API_URL = "https://api.blockscout.com/v2/api"

RPC_URL = os.getenv("ALCHEMY_RPC_URL") or "https://rpc.mainnet.chain.robinhood.com"

INPUT_FILE = Path("scanner_seen_qualifying_tokens.json")
FULL_OUTPUT_FILE = Path("conviction_holder_results.json")
TOP_OUTPUT_FILE = Path("top_conviction_holders.json")
CHECKPOINT_FILE = Path("conviction_holder_checkpoint.json")
PENDING_REVIEW_FILE = Path("conviction_pending_review.json")
TX_CACHE_FILE = Path("conviction_transaction_cache.json")
INTERNAL_CACHE_FILE = Path("conviction_internal_cache.json")
SALE_PROBE_CACHE_FILE = Path("conviction_rpc_sale_probe_cache.json")

TOP_HOLDER_LIMIT = 30

# Early-entry timing is retained as an informational metric.
# It will no longer be a mandatory holder qualification.
EARLY_WINDOW_SECONDS = 30 * 60

# Full results keep every qualifying holder.
# Live smart-wallet JSON keeps the earliest five per token.
TOP_CONVICTION_PER_TOKEN = 5

# 0 = analyse every pending token.
MAX_NEW_TOKENS_PER_RUN = 1

# Version 2 includes all major holders, not only confirmed DEX buyers.
ANALYSIS_VERSION = 2

# ProjectVex repeatedly caused prolonged Blockscout HTTP 500 loops in the
# existing analyser. Keep this targeted exclusion separate from retry logic.
SKIP_TOKEN_SYMBOLS = {"VEX", "CRCL"}

# Pagination / request safety.
PAGE_SIZE = 1000
MAX_TOKEN_PAGES_FOR_LAUNCH = 20
MAX_HOLDER_WALLET_PAGES = 10

MAX_RETRIES = 4
REQUEST_TIMEOUT = 30

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

LAST_HTTP_REQUEST_AT = 0.0

TX_CACHE: dict[str, dict[str, Any]] = {}
ADDRESS_CACHE: dict[str, dict[str, Any]] = {}
INTERNAL_CACHE: dict[str, list[dict[str, Any]]] = {}

FAILED_TX_HASHES: set[str] = set()
FAILED_INTERNAL_HASHES: set[str] = set()

TX_CACHE: dict[str, dict[str, Any]] = {}
INTERNAL_CACHE: dict[str, list[dict[str, Any]]] = {}
SALE_PROBE_CACHE: dict[str, dict[str, Any]] = {}
RPC_TOKEN_METADATA_CACHE: dict[str, dict[str, Any]] = {}
RPC_BLOCK_TIMESTAMP_CACHE: dict[int, str | None] = {}

API_FAILURE_COUNT = 0
API_FAILURE_LOG: list[dict[str, Any]] = []

TOP_BACKUP_CREATED = False


# =========================================================
# GENERAL HELPERS
# =========================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, str) and value.startswith("0x"):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalise_address(value: Any) -> str:
    address = str(value or "").lower()
    if address.startswith("0x") and len(address) == 42:
        return address
    return ""


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (ValueError, OSError):
            return None

    text = str(value).strip()

    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), timezone.utc)
        except (ValueError, OSError):
            return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def readable_time(value: Any) -> str | None:
    parsed = parse_time(value)
    return parsed.isoformat() if parsed else None


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "Unknown"

    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts = []

    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts[:3])


def format_number(value: Any) -> str:
    if value is None:
        return "Unknown"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    if abs(number) >= 1_000_000:
        return f"{number:,.2f}"

    return f"{number:,.8f}".rstrip("0").rstrip(".")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=4),
        encoding="utf-8",
    )


def report_contract(report: dict[str, Any]) -> str:
    token = report.get("token") or {}
    return normalise_address(token.get("contract"))


def register_api_failure(description: str, reason: str) -> None:
    global API_FAILURE_COUNT

    API_FAILURE_COUNT += 1

    API_FAILURE_LOG.append({
        "time_utc": utc_now(),
        "description": description,
        "reason": reason,
    })


def backup_top_database() -> None:
    global TOP_BACKUP_CREATED

    if TOP_BACKUP_CREATED or not TOP_OUTPUT_FILE.exists():
        return

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%d_%H%M%S")

    backup_path = Path(
        f"top_conviction_holders_backup_{timestamp}.json"
    )

    shutil.copy2(TOP_OUTPUT_FILE, backup_path)
    TOP_BACKUP_CREATED = True

    print(
        f"Cumulative conviction-holder backup created: "
        f"{backup_path}"
    )


# =========================================================
# HTTP / RPC
# =========================================================

def throttle_http_request() -> None:
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
    Blockscout request with PRO authentication, pacing and retry safety.

    Returns None on genuine API failure.
    Callers must NOT interpret None as an empty result.
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
                reason = f"authentication failed HTTP {response.status_code}"
                print(
                    f"{description}: Blockscout PRO authentication "
                    f"failed (HTTP {response.status_code})."
                )
                register_api_failure(description, reason)
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
                    reason = (
                        f"rate limited after {MAX_RETRIES} attempts"
                    )
                    print(
                        f"{description} remained rate limited. "
                        "Leaving this analysis incomplete."
                    )
                    register_api_failure(description, reason)
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
                    reason = f"server error {response.status_code}"

                    print(
                        f"{description} server error "
                        f"{response.status_code}. "
                        "Leaving this analysis incomplete."
                    )

                    register_api_failure(description, reason)
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
                    f"{description} failed after "
                    f"{MAX_RETRIES} attempts: {error}"
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

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < MAX_RETRIES:
                    time.sleep(wait_time)
                continue

            response.raise_for_status()
            data = response.json()

            if "error" not in data:
                return data.get("result")

        except (requests.RequestException, ValueError):
            pass

        if attempt < MAX_RETRIES:
            time.sleep(wait_time)

    global API_FAILURE_COUNT
    API_FAILURE_COUNT += 1
    return None


# =========================================================
# CONVICTION ANALYSER PERSISTENT CACHES
# =========================================================

def load_persistent_api_caches() -> None:
    """
    Load only conviction-analyser caches.

    These filenames are unique to this script and cannot overwrite or share
    state with early_buyer_analyzer.py or smart_wallet_tracker.py.
    """
    tx_data = read_json(TX_CACHE_FILE, {})
    internal_data = read_json(INTERNAL_CACHE_FILE, {})
    sale_probe_data = read_json(SALE_PROBE_CACHE_FILE, {})

    if isinstance(tx_data, dict):
        TX_CACHE.update({
            str(key).lower(): value
            for key, value in tx_data.items()
            if isinstance(value, dict)
        })

    if isinstance(internal_data, dict):
        INTERNAL_CACHE.update({
            str(key).lower(): value
            for key, value in internal_data.items()
            if isinstance(value, list)
        })

    if isinstance(sale_probe_data, dict):
        SALE_PROBE_CACHE.update({
            str(key): value
            for key, value in sale_probe_data.items()
            if isinstance(value, dict)
        })

    print(
        f"Conviction cache loaded: "
        f"{len(TX_CACHE)} transactions + "
        f"{len(INTERNAL_CACHE)} internal results + "
        f"{len(SALE_PROBE_CACHE)} RPC sale probes."
    )


def save_persistent_api_caches() -> None:
    write_json(TX_CACHE_FILE, TX_CACHE)
    write_json(INTERNAL_CACHE_FILE, INTERNAL_CACHE)
    write_json(SALE_PROBE_CACHE_FILE, SALE_PROBE_CACHE)


# =========================================================
# SCANNER INPUT / RESUME
# =========================================================

def load_scanner_tokens() -> list[dict[str, Any]]:
    data = read_json(INPUT_FILE, {})

    if isinstance(data, dict):
        tokens = data.get("tokens") or []
    elif isinstance(data, list):
        tokens = data
    else:
        tokens = []

    by_contract: dict[str, dict[str, Any]] = {}

    for token in tokens:
        if not isinstance(token, dict):
            continue

        contract = normalise_address(
            token.get("contract")
            or token.get("address_hash")
        )

        if not contract:
            continue

        symbol = str(
            token.get("symbol") or ""
        ).upper()

        if symbol in SKIP_TOKEN_SYMBOLS:
            continue

        item = dict(token)
        item["contract"] = contract

        by_contract[contract] = item

    return list(by_contract.values())


def load_existing_reports() -> list[dict[str, Any]]:
    data = read_json(FULL_OUTPUT_FILE, {})

    if not isinstance(data, dict):
        return []

    reports = data.get("reports") or []

    return [
        report
        for report in reports
        if isinstance(report, dict)
        and report_contract(report)
    ]


def report_is_complete(report: dict[str, Any]) -> bool:
    return report.get("token_analysis_complete") is True


def merge_reports(
    existing: list[dict[str, Any]],
    additions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_contract: dict[str, dict[str, Any]] = {}

    for report in existing:
        contract = report_contract(report)

        if contract:
            by_contract[contract] = report

    for report in additions:
        contract = report_contract(report)

        if contract:
            by_contract[contract] = report

    return list(by_contract.values())


def load_existing_top_results() -> list[dict[str, Any]]:
    data = read_json(TOP_OUTPUT_FILE, {})

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
    existing: list[dict[str, Any]],
    completed_reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    CUMULATIVE merge.

    Existing unrelated token results are preserved.
    Only a newly completed token is allowed to replace its own old record.
    """
    by_contract: dict[str, dict[str, Any]] = {}

    for result in existing:
        contract = report_contract(result)

        if contract:
            by_contract[contract] = result

    for report in completed_reports:
        contract = report_contract(report)

        if not contract:
            continue

        by_contract[contract] = {
            "token": report["token"],
            "launch": report["launch"],
            "analysis_version": report.get("analysis_version", 1),
            "token_analysis_complete": report.get("token_analysis_complete", False),
            "qualifying_holder_count": len(
                report.get("qualifying_holders") or []
            ),
            "top_conviction_holders": (
                report.get("top_conviction_holders") or []
            ),
            "holders_checked": (
                report.get("holders_checked") or []
            ),
        }

    return list(by_contract.values())


# =========================================================
# BLOCKSCOUT DATA
# =========================================================

def fetch_token_info(contract: str) -> dict[str, Any]:
    data = request_json(
        f"{PRO_REST_BASE_URL}/api/v2/tokens/{contract}",
        description="Token info",
    )

    return data if isinstance(data, dict) else {}


def fetch_transaction(transaction_hash: str) -> dict[str, Any]:
    transaction_hash = str(transaction_hash or "").lower()

    if not transaction_hash:
        return {}

    if transaction_hash in FAILED_TX_HASHES:
        return {}

    cached = TX_CACHE.get(transaction_hash)

    if isinstance(cached, dict):
        return cached

    data = request_json(
        f"{PRO_REST_BASE_URL}/api/v2/transactions/{transaction_hash}",
        description="Transaction",
    )

    if not isinstance(data, dict):
        FAILED_TX_HASHES.add(transaction_hash)
        return {}

    TX_CACHE[transaction_hash] = data

    if len(TX_CACHE) % 25 == 0:
        save_persistent_api_caches()

    return data


def fetch_address_info(address: str) -> dict[str, Any]:
    address = normalise_address(address)

    if not address:
        return {}

    if address in ADDRESS_CACHE:
        return ADDRESS_CACHE[address]

    data = request_json(
        f"{PRO_REST_BASE_URL}/api/v2/addresses/{address}",
        description="Address",
    )

    result = data if isinstance(data, dict) else {}
    ADDRESS_CACHE[address] = result

    return result


def fetch_internal_transactions(
    transaction_hash: str,
) -> list[dict[str, Any]]:
    transaction_hash = str(transaction_hash or "").lower()

    if not transaction_hash:
        return []

    if transaction_hash in FAILED_INTERNAL_HASHES:
        return []

    cached = INTERNAL_CACHE.get(transaction_hash)

    if isinstance(cached, list):
        return cached

    data = request_json(
        PRO_LEGACY_API_URL,
        params={
            "module": "account",
            "action": "txlistinternal",
            "txhash": transaction_hash,
        },
        description="Internal transaction",
    )

    if data is None:
        FAILED_INTERNAL_HASHES.add(transaction_hash)
        return []

    rows = data.get("result") or [] if isinstance(data, dict) else []

    if not isinstance(rows, list):
        FAILED_INTERNAL_HASHES.add(transaction_hash)
        return []

    INTERNAL_CACHE[transaction_hash] = rows

    if len(INTERNAL_CACHE) % 25 == 0:
        save_persistent_api_caches()

    return rows


def fetch_top_holders(
    contract: str,
    decimals: int,
) -> list[dict[str, Any]] | None:
    """
    Fetch up to 30 current holders from the Blockscout legacy PRO route.

    Contract filtering uses Robinhood RPC eth_getCode, avoiding the Blockscout
    address endpoint that was producing repeated HTTP 500/timeouts.
    """
    data = request_json(
        PRO_LEGACY_API_URL,
        params={
            "module": "token",
            "action": "getTokenHolders",
            "contractaddress": contract,
            "page": 1,
            "offset": TOP_HOLDER_LIMIT,
        },
        description="Token holders",
    )

    if data is None:
        return None

    if not isinstance(data, dict):
        register_api_failure("Token holders", "unexpected response format")
        return None

    rows = data.get("result")

    if not isinstance(rows, list):
        register_api_failure(
            "Token holders",
            "legacy endpoint returned non-list result",
        )
        return None

    holders = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        address = normalise_address(row.get("address"))

        if not address or address in ZERO_ADDRESSES:
            continue

        raw_value = safe_int(row.get("value"), 0)

        try:
            balance = raw_value / (10 ** int(decimals))
        except (ValueError, TypeError, OverflowError):
            balance = 0.0

        code = rpc_call(
            "eth_getCode",
            [address, "latest"],
        )

        is_contract = None

        if code is not None:
            code_text = str(code).lower()
            is_eip7702 = (
                code_text.startswith("0xef0100")
                and len(code_text) == 48
            )
            is_contract = (
                code_text not in {"0x", "0x0", ""}
                and not is_eip7702
            )

        if is_contract is not False:
            print(
                f"  Skipping holder {address}: "
                "wallet type could not be verified as an EOA."
            )
            continue

        holders.append({
            "wallet": address,
            "raw_balance": raw_value,
            "current_balance": balance,
            "is_contract": is_contract,
            "address_metadata": {},
        })

    holders.sort(
        key=lambda holder: holder["raw_balance"],
        reverse=True,
    )

    final_holders = holders[:TOP_HOLDER_LIMIT]

    for rank, holder in enumerate(final_holders, start=1):
        holder["holder_rank"] = rank

    return final_holders


def fetch_token_transfers(
    contract: str,
    address: str | None = None,
    start_block: int = 0,
    max_pages: int = 5,
    end_block: int = 99_999_999,
) -> list[dict[str, Any]] | None:
    """
    Etherscan-compatible token-transfer history using Blockscout PRO.

    sort=asc is critical because we need the holder's FIRST genuine buy.
    """
    collected = []
    seen_rows = set()

    for page in range(1, max_pages + 1):
        params = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract,
            "startblock": start_block,
            "endblock": end_block,
            "page": page,
            "offset": PAGE_SIZE,
            "sort": "asc",
        }

        if address:
            params["address"] = address

        data = request_json(
            PRO_LEGACY_API_URL,
            params=params,
            description=f"Token transfer page {page}",
        )

        if data is None:
            return None

        rows = data.get("result") or [] if isinstance(data, dict) else []

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
                    or row.get("transaction_hash")
                    or ""
                ).lower(),
                str(
                    row.get("logIndex")
                    or row.get("log_index")
                    or ""
                ),
                str(row.get("from") or "").lower(),
                str(row.get("to") or "").lower(),
                str(row.get("value") or ""),
            )

            if key in seen_rows:
                continue

            seen_rows.add(key)
            collected.append(row)

        if len(rows) < PAGE_SIZE:
            break

        if page == max_pages:
            print(
                f"  Transfer history reached its {max_pages}-page limit. "
                "Marking history incomplete rather than trusting "
                "potentially truncated results."
            )
            return None

        time.sleep(REQUEST_DELAY)

    collected.sort(
        key=lambda row: (
            safe_int(row.get("blockNumber")),
            safe_int(row.get("transactionIndex")),
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

def transaction_succeeded(transaction: dict[str, Any]) -> bool:
    return str(
        transaction.get("status") or ""
    ).lower() in {
        "ok",
        "success",
        "1",
    }


def transaction_method(transaction: dict[str, Any]) -> str:
    method = str(transaction.get("method") or "")

    if method:
        return method

    decoded = transaction.get("decoded_input") or {}

    method_call = str(decoded.get("method_call") or "")

    if method_call:
        return method_call

    raw_input = str(
        transaction.get("raw_input")
        or transaction.get("input")
        or "0x"
    ).lower()

    return raw_input[:10] if len(raw_input) >= 10 else raw_input


def is_swap(transaction: dict[str, Any]) -> bool:
    method = transaction_method(transaction).lower()

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

    selector = raw_input[:10] if len(raw_input) >= 10 else raw_input

    return selector in SWAP_SELECTORS


def token_contract_from_transfer(
    transfer: dict[str, Any],
) -> str:
    token = transfer.get("token") or {}

    return normalise_address(
        token.get("address_hash")
        or token.get("address")
        or transfer.get("contractAddress")
    )


def transfer_symbol(transfer: dict[str, Any]) -> str:
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
        or transfer.get("tokenDecimal"),
        fallback_decimals,
    )

    try:
        return int(raw_value) / (10 ** decimals)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def transaction_block(transaction: dict[str, Any]) -> int:
    return safe_int(
        transaction.get("block")
        or transaction.get("block_number")
        or transaction.get("blockNumber")
    )


def transaction_time(transaction: dict[str, Any]) -> str | None:
    return readable_time(
        transaction.get("timestamp")
        or transaction.get("timeStamp")
    )


def native_value_eth(transaction: dict[str, Any]) -> float:
    value = transaction.get("value") or 0

    if isinstance(value, dict):
        value = value.get("value") or value.get("raw") or 0

    try:
        raw_value = (
            int(value, 16)
            if isinstance(value, str) and value.startswith("0x")
            else int(value or 0)
        )
        return raw_value / 10 ** 18
    except (TypeError, ValueError):
        return 0.0


def internal_eth_received(
    transaction_hash: str,
    wallet: str,
) -> float:
    total = 0.0

    for row in fetch_internal_transactions(transaction_hash):
        recipient = normalise_address(row.get("to"))

        if recipient != wallet:
            continue

        if str(row.get("isError") or "0") == "1":
            continue

        try:
            total += int(row.get("value") or 0) / 10 ** 18
        except (TypeError, ValueError):
            continue

    return total


def quote_flows(
    transaction: dict[str, Any],
    transaction_hash: str,
    wallet: str,
    target_contract: str,
    include_internal_eth: bool,
) -> dict[str, dict[str, float]]:
    incoming: defaultdict[str, float] = defaultdict(float)
    outgoing: defaultdict[str, float] = defaultdict(float)

    if normalise_address(transaction.get("from")) == wallet:
        native_sent = native_value_eth(transaction)

        if native_sent > 0:
            outgoing["ETH"] += native_sent

    if include_internal_eth:
        native_received = internal_eth_received(
            transaction_hash,
            wallet,
        )

        if native_received > 0:
            incoming["ETH"] += native_received

    for transfer in transaction.get("token_transfers") or []:
        if token_contract_from_transfer(transfer) == target_contract:
            continue

        amount = transfer_amount(transfer)

        if amount <= 0:
            continue

        symbol = transfer_symbol(transfer)
        sender = normalise_address(transfer.get("from"))
        recipient = normalise_address(transfer.get("to"))

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


def row_amount(
    row: dict[str, Any],
    decimals: int,
) -> float:
    try:
        return safe_int(row.get("value")) / (
            10 ** safe_int(
                row.get("tokenDecimal"),
                decimals,
            )
        )
    except (TypeError, ValueError, OverflowError):
        return 0.0


# =========================================================
# RPC ENTRY / BUY DISCOVERY
# =========================================================

def rpc_transaction_bundle(
    transaction_hash: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    transaction_hash = str(transaction_hash or "").lower()

    transaction = rpc_call(
        "eth_getTransactionByHash",
        [transaction_hash],
    )

    if not isinstance(transaction, dict):
        return None, None

    receipt = rpc_call(
        "eth_getTransactionReceipt",
        [transaction_hash],
    )

    if not isinstance(receipt, dict):
        return None, None

    if rpc_hex_to_int(receipt.get("status"), 0) != 1:
        return None, None

    return transaction, receipt


def rpc_transaction_selector(
    transaction: dict[str, Any],
) -> str:
    raw_input = str(transaction.get("input") or "0x").lower()
    return raw_input[:10] if len(raw_input) >= 10 else raw_input


def rpc_is_swap_transaction(
    transaction: dict[str, Any],
) -> bool:
    return rpc_transaction_selector(transaction) in SWAP_SELECTORS


def rpc_receipt_token_flows(
    receipt: dict[str, Any],
    wallet: str,
) -> dict[str, dict[str, int]]:
    wallet = wallet.lower()
    flows: dict[str, dict[str, int]] = {}

    for log in receipt.get("logs") or []:
        transfer = rpc_decode_transfer_log(log)

        if transfer is None:
            continue

        flow = flows.setdefault(
            transfer["contract"],
            {"received_raw": 0, "sent_raw": 0, "net_raw": 0},
        )

        if transfer["recipient"] == wallet:
            flow["received_raw"] += transfer["raw_amount"]

        if transfer["sender"] == wallet:
            flow["sent_raw"] += transfer["raw_amount"]

    for flow in flows.values():
        flow["net_raw"] = flow["received_raw"] - flow["sent_raw"]

    return flows


def rpc_target_tokens_received(
    receipt: dict[str, Any],
    wallet: str,
    target_contract: str,
    decimals: int,
) -> float:
    flow = rpc_receipt_token_flows(receipt, wallet).get(target_contract.lower())

    if not flow or flow["net_raw"] <= 0:
        return 0.0

    try:
        return flow["net_raw"] / (10 ** int(decimals))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def rpc_quote_spend_for_buy(
    transaction: dict[str, Any],
    receipt: dict[str, Any],
    wallet: str,
    target_contract: str,
) -> tuple[float, float]:
    wallet = wallet.lower()
    target_contract = target_contract.lower()

    native_eth = 0.0
    wrapped_eth = 0.0
    spent_stable = 0.0
    unsupported_outgoing = False

    if normalise_address(transaction.get("from")) == wallet:
        native_eth = rpc_hex_to_int(
            transaction.get("value"),
            0,
        ) / 10 ** 18

    flows = rpc_receipt_token_flows(receipt, wallet)

    for contract, flow in flows.items():
        if contract == target_contract or flow["net_raw"] >= 0:
            continue

        metadata = rpc_token_metadata(contract)
        symbol = str(metadata.get("symbol") or "UNKNOWN").upper()
        decimals = safe_int(metadata.get("decimals"), 18)

        try:
            amount = abs(flow["net_raw"]) / (10 ** decimals)
        except (TypeError, ValueError, OverflowError):
            amount = 0.0

        if symbol in ETH_SYMBOLS:
            wrapped_eth += amount
        elif symbol in STABLE_SYMBOLS:
            spent_stable += amount
        else:
            unsupported_outgoing = True

    # Reject ambiguous payment flows rather than invent a cost basis.
    if unsupported_outgoing or (native_eth > 0 and wrapped_eth > 0):
        return 0.0, max(spent_stable, 1.0)

    return native_eth + wrapped_eth, spent_stable


# =========================================================
# LAUNCH DISCOVERY
# =========================================================

def find_first_confirmed_buy(
    rows: list[dict[str, Any]],
    contract: str,
    decimals: int,
) -> dict[str, Any] | None:
    """Find the first genuine DEX buy using Robinhood RPC transaction data."""
    seen_transactions = set()

    for row in rows:
        tx_hash = str(
            row.get("hash")
            or row.get("transaction_hash")
            or ""
        ).lower()

        if not tx_hash or tx_hash in seen_transactions:
            continue

        seen_transactions.add(tx_hash)

        transaction, receipt = rpc_transaction_bundle(tx_hash)

        if (
            transaction is None
            or receipt is None
            or not rpc_is_swap_transaction(transaction)
        ):
            continue

        wallet = normalise_address(transaction.get("from"))

        if not wallet or wallet in ZERO_ADDRESSES:
            continue

        received = rpc_target_tokens_received(
            receipt,
            wallet,
            contract,
            decimals,
        )

        if received <= 0:
            continue

        spent_eth, spent_stable = rpc_quote_spend_for_buy(
            transaction,
            receipt,
            wallet,
            contract,
        )

        if spent_eth <= 0 and spent_stable <= 0:
            continue

        block_number = rpc_hex_to_int(
            receipt.get("blockNumber")
            or transaction.get("blockNumber"),
            0,
        )

        return {
            "wallet": wallet,
            "transaction_hash": tx_hash,
            "entry_block": block_number,
            "entry_time_utc": (
                rpc_block_timestamp(block_number)
                if block_number > 0
                else None
            ),
            "tokens_received": received,
            "spent_eth": spent_eth,
            "spent_stable": spent_stable,
            "method": rpc_transaction_selector(transaction),
        }

    return None


# =========================================================
# CURRENT HOLDER FIRST-BUY DISCOVERY
# =========================================================

def find_holder_first_buy(
    wallet: str,
    rows: list[dict[str, Any]],
    contract: str,
    decimals: int,
) -> dict[str, Any] | None:
    """Find a Top-20 holder's first genuine DEX buy using Robinhood RPC."""
    wallet = wallet.lower()
    seen_transactions = set()

    for row in rows:
        if normalise_address(row.get("to")) != wallet:
            continue

        tx_hash = str(
            row.get("hash")
            or row.get("transaction_hash")
            or ""
        ).lower()

        if not tx_hash or tx_hash in seen_transactions:
            continue

        seen_transactions.add(tx_hash)

        transaction, receipt = rpc_transaction_bundle(tx_hash)

        if (
            transaction is None
            or receipt is None
            or not rpc_is_swap_transaction(transaction)
        ):
            continue

        if normalise_address(transaction.get("from")) != wallet:
            continue

        received = rpc_target_tokens_received(
            receipt,
            wallet,
            contract,
            decimals,
        )

        if received <= 0:
            continue

        spent_eth, spent_stable = rpc_quote_spend_for_buy(
            transaction,
            receipt,
            wallet,
            contract,
        )

        if spent_eth <= 0 and spent_stable <= 0:
            continue

        block_number = rpc_hex_to_int(
            receipt.get("blockNumber")
            or transaction.get("blockNumber"),
            0,
        )

        return {
            "wallet": wallet,
            "entry_transaction_hash": tx_hash,
            "entry_block": block_number,
            "entry_time_utc": (
                rpc_block_timestamp(block_number)
                if block_number > 0
                else None
            ),
            "initial_tokens_bought": received,
            "initial_spent_eth": spent_eth,
            "initial_spent_stable": spent_stable,
            "entry_method": rpc_transaction_selector(transaction),
        }

    return None


# =========================================================
# RPC SALE PROBING
# =========================================================

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

    return normalise_address("0x" + value[-40:])


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
            offset = int.from_bytes(raw_bytes[0:32], "big")

            if offset + 32 <= len(raw_bytes):
                length = int.from_bytes(
                    raw_bytes[offset:offset + 32],
                    "big",
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
        return raw_bytes.rstrip(b"\x00").decode(
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


def rpc_token_balance(
    contract: str,
    wallet: str,
    decimals: int,
    block: str = "latest",
) -> float | None:
    """Read an ERC-20 balance directly from Robinhood RPC."""
    contract = normalise_address(contract)
    wallet = normalise_address(wallet)

    if not contract or not wallet:
        return None

    # ERC-20 balanceOf(address) selector.
    call_data = "0x70a08231" + wallet[2:].lower().zfill(64)

    result = rpc_call(
        "eth_call",
        [
            {
                "to": contract,
                "data": call_data,
            },
            block,
        ],
    )

    if not isinstance(result, str) or result in {"", "0x"}:
        return None

    try:
        return int(result, 16) / (10 ** int(decimals))
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return None


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

    result = {
        "symbol": str(symbol).upper(),
        "decimals": decimals,
    }

    RPC_TOKEN_METADATA_CACHE[contract] = result
    return result


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




def rpc_wallet_asset_flows(
    transaction: dict[str, Any],
    receipt: dict[str, Any],
    wallet: str,
    target_contract: str,
) -> dict[str, Any]:
    """Record wallet-side ERC20 and native ETH flows without valuing them."""
    wallet = normalise_address(wallet)
    target_contract = normalise_address(target_contract)

    assets = []

    for contract, flow in rpc_receipt_token_flows(receipt, wallet).items():
        if contract == target_contract:
            continue

        net_raw = flow["net_raw"]
        if net_raw == 0:
            continue

        metadata = rpc_token_metadata(contract)
        decimals = safe_int(metadata.get("decimals"), 18)

        assets.append({
            "contract": contract,
            "symbol": str(metadata.get("symbol") or "UNKNOWN").upper(),
            "amount": abs(net_raw) / (10 ** decimals),
            "direction": "received" if net_raw > 0 else "spent",
        })

    if normalise_address(transaction.get("from")) == wallet:
        native_eth = rpc_hex_to_int(transaction.get("value"), 0) / 10 ** 18

        if native_eth > 0:
            assets.append({
                "contract": None,
                "symbol": "ETH",
                "amount": native_eth,
                "direction": "spent",
            })

    return {
        "assets": assets,
        "valuation_status": "NOT_VALUED",
    }




def classify_multi_asset_value(asset_flows):
    """Classify swap assets without inventing historical prices."""
    eth_value = 0.0
    stable_value = 0.0
    other_assets = []

    for asset in asset_flows.get("assets", []):
        symbol = str(asset.get("symbol") or "").upper()
        amount = safe_float(asset.get("amount"))
        direction = asset.get("direction")

        if amount <= 0:
            continue

        signed_amount = amount if direction == "received" else -amount

        if symbol in ETH_SYMBOLS:
            eth_value += signed_amount
        elif symbol in STABLE_SYMBOLS:
            stable_value += signed_amount
        else:
            other_assets.append(asset)

    return {
        "net_eth": eth_value,
        "net_stable": stable_value,
        "other_assets": other_assets,
        "historical_valuation_complete": not other_assets and stable_value == 0,
    }


def rpc_probe_sale_candidate(
    transaction_hash: str,
    wallet: str,
    target_contract: str,
) -> dict[str, Any]:
    """
    Use Robinhood RPC to classify a target-token outbound transaction.

    This avoids the high-volume Blockscout /transactions/{hash} pattern that
    caused repeated HTTP 500 errors in the early-buyer analyser.
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

    cached = SALE_PROBE_CACHE.get(cache_key)

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
        }

        SALE_PROBE_CACHE[cache_key] = result
        return result

    target_out_seen = False
    incoming_quote: defaultdict[str, float] = defaultdict(float)

    for log in receipt.get("logs") or []:
        transfer = rpc_decode_transfer_log(log)

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

    unsupported_proceeds = {
        symbol: amount
        for symbol, amount in incoming_quote.items()
        if symbol not in ETH_SYMBOLS
        and symbol not in STABLE_SYMBOLS
        and amount > 0
    }

    if unsupported_proceeds:
        result = {
            "probe_complete": True,
            "is_confirmed_sale": False,
            "reason": "Unsupported or mixed sale proceeds",
            "unsupported_proceeds": unsupported_proceeds,
        }
        SALE_PROBE_CACHE[cache_key] = result
        return result

    used_internal_fallback = False

    if (
        received_eth <= 0
        and received_stable <= 0
    ):
        failure_count_before = API_FAILURE_COUNT

        native_eth = internal_eth_received(
            transaction_hash,
            wallet,
        )

        if native_eth > 0:
            received_eth = native_eth
            used_internal_fallback = True

        elif API_FAILURE_COUNT > failure_count_before:
            return {
                "probe_complete": False,
                "reason": (
                    "Confirmed swap but native ETH "
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
        }

        SALE_PROBE_CACHE[cache_key] = result
        return result

    block_number = rpc_hex_to_int(
        receipt.get("blockNumber")
        or transaction.get("blockNumber"),
        0,
    )

    quote_flows = {
        "incoming": dict(incoming_quote),
        "outgoing": {},
    }

    if used_internal_fallback:
        quote_flows["incoming"]["ETH"] = received_eth

    result = {
        "probe_complete": True,
        "is_confirmed_sale": True,
        "received_eth": received_eth,
        "received_stable": received_stable,
        "timestamp_utc": (
            rpc_block_timestamp(block_number)
            if block_number > 0
            else None
        ),
        "method": selector,
        "quote_flows": quote_flows,
        "used_internal_eth_fallback": used_internal_fallback,
    }

    SALE_PROBE_CACHE[cache_key] = result

    if len(SALE_PROBE_CACHE) % 25 == 0:
        save_persistent_api_caches()

    return result


# =========================================================
# HOLDER SALE / PROFIT ANALYSIS
# =========================================================

def outgoing_transactions_after_entry(
    rows: list[dict[str, Any]],
    wallet: str,
    entry_block: int,
    entry_transaction_hash: str,
    decimals: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for row in rows:
        if normalise_address(row.get("from")) != wallet:
            continue

        block_number = safe_int(row.get("blockNumber"))

        transaction_hash = str(
            row.get("hash")
            or row.get("transaction_hash")
            or ""
        ).lower()

        if not transaction_hash:
            continue

        if (
            block_number < entry_block
            or transaction_hash == entry_transaction_hash
        ):
            continue

        if transaction_hash not in grouped:
            grouped[transaction_hash] = {
                "transaction_hash": transaction_hash,
                "block_number": block_number,
                "transaction_index": safe_int(
                    row.get("transactionIndex")
                ),
                "tokens_out": 0.0,
            }

        grouped[transaction_hash]["tokens_out"] += row_amount(
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



def group_holder_token_transactions(
    wallet: str,
    wallet_rows: list[dict[str, Any]],
    decimals: int,
) -> list[dict[str, Any]]:
    """Group a holder's token transfers by transaction, chronologically."""
    wallet = normalise_address(wallet)
    grouped = {}

    for row in wallet_rows:
        tx_hash = str(
            row.get("hash")
            or row.get("transaction_hash")
            or ""
        ).lower()

        if not tx_hash:
            continue

        sender = normalise_address(row.get("from"))
        recipient = normalise_address(row.get("to"))

        if sender != wallet and recipient != wallet:
            continue

        amount = row_amount(row, decimals)

        if tx_hash not in grouped:
            grouped[tx_hash] = {
                "transaction_hash": tx_hash,
                "block_number": safe_int(row.get("blockNumber")),
                "transaction_index": safe_int(
                    row.get("transactionIndex")
                ),
                "tokens_in": 0.0,
                "tokens_out": 0.0,
            }

        if recipient == wallet:
            grouped[tx_hash]["tokens_in"] += amount

        if sender == wallet:
            grouped[tx_hash]["tokens_out"] += amount

    return sorted(
        grouped.values(),
        key=lambda item: (
            item["block_number"],
            item["transaction_index"],
            item["transaction_hash"],
        ),
    )



def calculate_position_profit(
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Calculate realised profit using weighted-average cost.

    Trades must already be verified and denominated in ETH.
    Non-trade transfers and mixed quote assets must be resolved
    before this calculator is used.
    """
    tokens_held = 0.0
    remaining_cost_eth = 0.0
    total_bought = 0.0
    total_sold = 0.0
    total_spent_eth = 0.0
    total_received_eth = 0.0
    realised_profit_eth = 0.0
    realised_cost_eth = 0.0
    buy_count = 0
    sell_count = 0

    for trade in trades:
        side = trade.get("side")
        tokens = safe_float(trade.get("tokens"))
        eth = safe_float(trade.get("eth"))

        if side not in {"buy", "sell"}:
            raise ValueError("Unknown trade side")

        if tokens <= 0 or eth <= 0:
            raise ValueError("Trade amounts must be positive")

        if side == "buy":
            tokens_held += tokens
            remaining_cost_eth += eth
            total_bought += tokens
            total_spent_eth += eth
            buy_count += 1
            continue

        if tokens > tokens_held + 1e-9:
            raise ValueError(
                "Sale exceeds reconstructed holdings; "
                "position requires reconciliation"
            )

        average_cost = remaining_cost_eth / tokens_held
        allocated_cost = average_cost * tokens

        realised_cost_eth += allocated_cost
        realised_profit_eth += eth - allocated_cost
        total_received_eth += eth
        total_sold += tokens
        sell_count += 1

        tokens_held = max(0.0, tokens_held - tokens)
        remaining_cost_eth = max(
            0.0,
            remaining_cost_eth - allocated_cost,
        )

    realised_roi = (
        realised_profit_eth / realised_cost_eth * 100.0
        if realised_cost_eth > 0
        else None
    )

    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_tokens_bought": total_bought,
        "total_tokens_sold": total_sold,
        "total_spent_eth": total_spent_eth,
        "total_received_eth": total_received_eth,
        "reconstructed_tokens_held": tokens_held,
        "remaining_cost_eth": remaining_cost_eth,
        "realised_profit_eth": realised_profit_eth,
        "realised_roi_percent": realised_roi,
        "calculation_method": "weighted_average_cost",
    }





def calculate_multi_asset_position_profit(
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Calculate weighted-average realised profit from trades
    with independently verified ETH-equivalent valuations.

    Never estimate missing historical prices.
    """
    converted = []
    missing_valuations = []

    for trade in trades:
        eth_value = trade.get("verified_eth_value")

        if eth_value is None or safe_float(eth_value) <= 0:
            missing_valuations.append({
                "transaction_hash": trade.get("transaction_hash"),
                "reason": "Historical ETH valuation unavailable",
            })
            continue

        converted.append({
            "side": trade["side"],
            "tokens": trade["tokens"],
            "eth": safe_float(eth_value),
        })

    if missing_valuations:
        return {
            "profit_status": "UNVERIFIED",
            "reason": "One or more trade valuations unavailable",
            "missing_valuations": missing_valuations,
            "valued_trade_count": len(converted),
        }

    calculation = calculate_position_profit(converted)

    return {
        "profit_status": "VALUED",
        "valuation_currency": "ETH",
        **calculation,
    }




def stablecoin_to_eth_value(
    stable_amount: float,
    historical_eth_usd: float,
    stable_usd_price: float = 1.0,
) -> float:
    """Convert stablecoin proceeds using supplied historical prices."""
    amount = safe_float(stable_amount)
    eth_usd = safe_float(historical_eth_usd)
    stable_usd = safe_float(stable_usd_price)

    if amount <= 0 or eth_usd <= 0 or stable_usd <= 0:
        raise ValueError("Verified positive historical prices required")

    return (amount * stable_usd) / eth_usd



def calculate_whale_holdings(wallet, wallet_rows, decimals, current_balance):
    """Summarise observed token movements without assuming they are trades."""
    grouped = group_holder_token_transactions(
        normalise_address(wallet), wallet_rows, decimals
    )

    received = sum(
        max(0.0, safe_float(item.get("tokens_in")))
        for item in grouped
    )
    sent = sum(
        max(0.0, safe_float(item.get("tokens_out")))
        for item in grouped
    )

    balance = safe_float(current_balance)

    first_inbound = next(
        (
            item for item in grouped
            if safe_float(item.get("tokens_in")) > 0
        ),
        None,
    )

    return {
        "first_acquisition_block": (
            first_inbound["block_number"]
            if first_inbound else None
        ),
        "first_acquisition_transaction": (
            first_inbound["transaction_hash"]
            if first_inbound else None
        ),
        "first_acquisition_method": "OBSERVED TOKEN INBOUND",
        "total_tokens_received": received,
        "total_tokens_sent": sent,
        "current_tokens_held": balance,
        "retained_percentage_of_received": (
            balance / received * 100.0 if received > 0 else None
        ),
        "sales_not_inferred_from_transfers": True,
    }


def reconstruct_holder_position(
    wallet: str,
    wallet_rows: list[dict[str, Any]],
    contract: str,
    decimals: int,
    current_balance: float,
    snapshot_block: int | None = None,
    history_start_block: int | None = None,
) -> dict[str, Any]:
    """Reconstruct an ETH-denominated position conservatively."""
    wallet = normalise_address(wallet)

    if history_start_block is not None:
        if history_start_block <= 0:
            return {
                "position_status": "INCOMPLETE",
                "reason": "Invalid history starting block",
            }

        opening_balance = rpc_token_balance(
            contract=contract,
            wallet=wallet,
            decimals=decimals,
            block=hex(history_start_block - 1),
        )

        if opening_balance is None:
            return {
                "position_status": "INCOMPLETE",
                "reason": "Historical opening balance unavailable",
            }

        if opening_balance > 1e-8:
            return {
                "position_status": "UNVERIFIED",
                "reason": "Wallet held tokens before available history",
                "opening_balance": opening_balance,
            }

    grouped = group_holder_token_transactions(
        wallet, wallet_rows, decimals
    )
    trades = []
    unresolved = []
    unresolved_details = []

    def mark_unresolved(tx_hash, reason, **details):
        unresolved.append(tx_hash)
        unresolved_details.append({
            "transaction_hash": tx_hash,
            "reason": reason,
            **details,
        })

    for item in grouped:
        tx_hash = item["transaction_hash"]
        tokens_in = safe_float(item["tokens_in"])
        tokens_out = safe_float(item["tokens_out"])

        if tokens_in <= 0 and tokens_out <= 0:
            continue

        # Complex same-transaction movements cannot safely be treated
        # as a simple purchase or sale.
        if tokens_in > 0 and tokens_out > 0:
            mark_unresolved(tx_hash, "Mixed incoming and outgoing target tokens")
            continue

        transaction, receipt = rpc_transaction_bundle(tx_hash)

        if transaction is None or receipt is None:
            return {
                "position_status": "INCOMPLETE",
                "reason": "RPC transaction or receipt unavailable",
                "transaction_hash": tx_hash,
            }

        if normalise_address(transaction.get("from")) != wallet:
            mark_unresolved(tx_hash, "Wallet was not transaction initiator")
            continue

        if not rpc_is_swap_transaction(transaction):
            mark_unresolved(tx_hash, "Unrecognised swap selector")
            continue

        asset_flows = rpc_wallet_asset_flows(
            transaction, receipt, wallet, contract
        )
        asset_flows["valuation"] = classify_multi_asset_value(
            asset_flows
        )

        net_received = rpc_target_tokens_received(
            receipt, wallet, contract, decimals
        )

        if net_received > 0:
            spent_eth, spent_stable = rpc_quote_spend_for_buy(
                transaction, receipt, wallet, contract
            )

            if spent_eth > 0 and spent_stable == 0:
                trades.append({
                    "side": "buy",
                    "tokens": net_received,
                    "eth": spent_eth,
                })
            else:
                mark_unresolved(
                    tx_hash,
                    "Purchase quote asset not eligible for ETH profit calculation",
                    spent_eth=spent_eth,
                    spent_stable=spent_stable,
                    asset_flows=asset_flows,
                )

            continue

        if tokens_out > 0:
            probe = rpc_probe_sale_candidate(
                tx_hash, wallet, contract
            )

            if not probe.get("probe_complete"):
                return {
                    "position_status": "INCOMPLETE",
                    "reason": "Sale verification unavailable",
                    "transaction_hash": tx_hash,
                }

            if (
                probe.get("is_confirmed_sale")
                and safe_float(probe.get("received_eth")) > 0
                and safe_float(probe.get("received_stable")) == 0
            ):
                trades.append({
                    "side": "sell",
                    "tokens": tokens_out,
                    "eth": safe_float(probe["received_eth"]),
                })
            else:
                mark_unresolved(
                    tx_hash,
                    probe.get("reason") or "Sale not eligible for ETH profit calculation",
                    received_eth=safe_float(probe.get("received_eth")),
                    received_stable=safe_float(probe.get("received_stable")),
                    unsupported_proceeds=probe.get("unsupported_proceeds") or {},
                    asset_flows=asset_flows,
                )

    if unresolved:
        valued_later = []
        genuinely_unverified = []

        for item in unresolved_details:
            flows = item.get("asset_flows") or {}
            assets = flows.get("assets") or []

            if assets and (
                item.get("reason") in {
                    "Purchase quote asset not eligible for ETH profit calculation",
                    "Sale not eligible for ETH profit calculation",
                    "Unsupported or mixed sale proceeds",
                }
            ):
                valued_later.append(item)
            else:
                genuinely_unverified.append(item)

        return {
            "position_status": "UNVERIFIED",
            "reason": "Position requires multi-asset valuation or transfer reconciliation",
            "unresolved_transactions": unresolved,
            "unresolved_details": unresolved_details,
            "verified_eth_trade_count": len(trades),
            "multi_asset_valuation_candidates": valued_later,
            "other_unverified_transactions": genuinely_unverified,
            "multi_asset_candidate_count": len(valued_later),
            "other_unverified_count": len(genuinely_unverified),
        }

    try:
        calculation = calculate_position_profit(trades)
    except ValueError as exc:
        return {
            "position_status": "UNVERIFIED",
            "reason": str(exc),
        }

    reconstructed = safe_float(
        calculation["reconstructed_tokens_held"]
    )

    rpc_balance = rpc_token_balance(
        contract=contract,
        wallet=wallet,
        decimals=decimals,
        block=hex(snapshot_block) if snapshot_block is not None else "latest",
    )

    if rpc_balance is None:
        return {
            "position_status": "INCOMPLETE",
            "reason": "Independent RPC token balance unavailable",
        }

    tolerance = max(
        1e-8,
        abs(rpc_balance) * 0.001,
    )

    if abs(reconstructed - rpc_balance) > tolerance:
        return {
            "position_status": "UNVERIFIED",
            "reason": "Reconstructed holdings differ from RPC token balance",
            "reconstructed_balance": reconstructed,
            "rpc_balance": rpc_balance,
        }

    return {
        "position_status": "VERIFIED",
        "reason": "ETH-denominated position reconciled to RPC balance",
        **calculation,
        "current_balance": current_balance,
        "rpc_verified_balance": rpc_balance,
        "snapshot_block": snapshot_block,
        "blockscout_balance_is_informational": True,
    }


def analyse_holder_sales(
    first_buy: dict[str, Any],
    wallet_rows: list[dict[str, Any]],
    contract: str,
    decimals: int,
    current_balance: float,
) -> dict[str, Any]:
    """
    Analyse sales after the holder's first early buy using RPC sale probes.

    Qualification itself is unchanged:
      current Top-20 holder + first genuine buy within 30 minutes.

    Sale/profit analysis is descriptive only and cannot remove an otherwise
    qualifying conviction holder.
    """
    wallet = first_buy["wallet"]

    initial_tokens = safe_float(
        first_buy.get("initial_tokens_bought")
    )

    initial_spent_eth = safe_float(
        first_buy.get("initial_spent_eth")
    )

    initial_spent_stable = safe_float(
        first_buy.get("initial_spent_stable")
    )

    entry_block = safe_int(
        first_buy.get("entry_block")
    )

    entry_hash = str(
        first_buy.get("entry_transaction_hash") or ""
    ).lower()

    candidates = outgoing_transactions_after_entry(
        rows=wallet_rows,
        wallet=wallet,
        entry_block=entry_block,
        entry_transaction_hash=entry_hash,
        decimals=decimals,
    )

    confirmed_sells = []

    original_tokens_counted_sold = 0.0
    received_eth_for_original = 0.0
    received_stable_for_original = 0.0
    sale_analysis_complete = True
    unresolved_sale_transactions = []

    for candidate_index, candidate in enumerate(
        candidates,
        start=1,
    ):
        if (
            initial_tokens > 0
            and original_tokens_counted_sold >= initial_tokens
        ):
            break

        tx_hash = candidate["transaction_hash"]

        print(
            f"      RPC sale probe "
            f"{candidate_index}/{len(candidates)}",
            end="\r",
            flush=True,
        )

        probe = rpc_probe_sale_candidate(
            transaction_hash=tx_hash,
            wallet=wallet,
            target_contract=contract,
        )

        if not probe.get("probe_complete"):
            sale_analysis_complete = False
            unresolved_sale_transactions.append(
                tx_hash
            )
            print(
                f"\n      Sale probe unavailable for "
                f"{tx_hash}: "
                f"{probe.get('reason', 'unknown reason')}. "
                "Sale metrics for this holder are marked incomplete."
            )
            break

        if not probe.get("is_confirmed_sale"):
            continue

        tokens_out = safe_float(
            candidate.get("tokens_out")
        )

        if tokens_out <= 0:
            continue

        remaining_original = max(
            0.0,
            initial_tokens
            - original_tokens_counted_sold,
        )

        tokens_counted = (
            min(tokens_out, remaining_original)
            if initial_tokens > 0
            else 0.0
        )

        fraction_counted = (
            tokens_counted / tokens_out
            if tokens_out > 0
            else 0.0
        )

        counted_eth = (
            safe_float(probe.get("received_eth"))
            * fraction_counted
        )

        counted_stable = (
            safe_float(probe.get("received_stable"))
            * fraction_counted
        )

        original_tokens_counted_sold += (
            tokens_counted
        )

        received_eth_for_original += (
            counted_eth
        )

        received_stable_for_original += (
            counted_stable
        )

        confirmed_sells.append({
            "transaction_hash": tx_hash,
            "block_number": candidate["block_number"],
            "tokens_out": tokens_out,
            "tokens_out_counted_against_original_buy": tokens_counted,
            "received_eth_counted_against_original_buy": counted_eth,
            "received_stable_counted_against_original_buy": counted_stable,
            "timestamp_utc": probe.get("timestamp_utc"),
            "method": probe.get("method"),
            "classification_source": "Robinhood RPC receipt",
            "used_internal_eth_fallback": probe.get(
                "used_internal_eth_fallback",
                False,
            ),
        })

    if candidates:
        print(" " * 80, end="\r")

    percentage_original_sold = (
        min(
            100.0,
            (
                original_tokens_counted_sold
                / initial_tokens
            ) * 100.0,
        )
        if initial_tokens > 0
        else 0.0
    )

    current_as_pct_initial = (
        (
            current_balance
            / initial_tokens
        ) * 100.0
        if initial_tokens > 0
        else None
    )

    fraction_original_sold = (
        min(
            1.0,
            original_tokens_counted_sold
            / initial_tokens,
        )
        if initial_tokens > 0
        else 0.0
    )

    cost_basis_eth = (
        initial_spent_eth
        * fraction_original_sold
    )

    cost_basis_stable = (
        initial_spent_stable
        * fraction_original_sold
    )

    realised_profit = None
    realised_roi = None
    ranking_asset = None

    if not sale_analysis_complete:
        profit_status = (
            "Sale analysis incomplete due to unresolved RPC/internal lookup"
        )

    elif (
        initial_spent_eth > 0
        and initial_spent_stable == 0
        and received_stable_for_original == 0
    ):
        realised_profit = (
            received_eth_for_original
            - cost_basis_eth
        )
        ranking_asset = "ETH"

        if cost_basis_eth > 0:
            realised_roi = (
                realised_profit
                / cost_basis_eth
            ) * 100.0

        profit_status = (
            "Original early allocation realised ETH/WETH estimate"
        )

    elif (
        initial_spent_stable > 0
        and initial_spent_eth == 0
        and received_eth_for_original == 0
    ):
        realised_profit = (
            received_stable_for_original
            - cost_basis_stable
        )
        ranking_asset = "STABLE"

        if cost_basis_stable > 0:
            realised_roi = (
                realised_profit
                / cost_basis_stable
            ) * 100.0

        profit_status = (
            "Original early allocation realised stablecoin estimate"
        )

    elif confirmed_sells:
        profit_status = (
            "Mixed quote assets — proceeds recorded but exact "
            "single-asset profit intentionally not inferred"
        )

    else:
        profit_status = (
            "No confirmed sale of original early allocation"
        )

    first_exit_time = (
        confirmed_sells[0].get("timestamp_utc")
        if confirmed_sells
        else None
    )

    last_exit_time = (
        confirmed_sells[-1].get("timestamp_utc")
        if confirmed_sells
        else None
    )

    return {
        "confirmed_sell_count": len(confirmed_sells),
        "confirmed_sells": confirmed_sells,
        "original_tokens_counted_sold": (
            original_tokens_counted_sold
        ),
        "percentage_of_original_early_buy_sold": (
            percentage_original_sold
        ),
        "current_balance": current_balance,
        "current_balance_as_percentage_of_initial_early_buy": (
            current_as_pct_initial
        ),
        "realised_eth_from_original_early_buy": (
            received_eth_for_original
        ),
        "realised_stable_from_original_early_buy": (
            received_stable_for_original
        ),
        "realised_profit": realised_profit,
        "realised_roi": realised_roi,
        "ranking_asset": ranking_asset,
        "profit_status": profit_status,
        "first_exit_time_utc": first_exit_time,
        "last_exit_time_utc": last_exit_time,
        "sale_analysis_complete": sale_analysis_complete,
        "unresolved_sale_transactions": (
            unresolved_sale_transactions
        ),
    }


# =========================================================
# CHECKPOINT / SAVING
# =========================================================


def load_pending_reviews():
    if not PENDING_REVIEW_FILE.exists():
        return {}
    data = json.loads(PENDING_REVIEW_FILE.read_text())
    return data if isinstance(data, dict) else {}


def save_pending_review():
    checkpoint = json.loads(CHECKPOINT_FILE.read_text())
    contract = normalise_address(
        checkpoint.get("token", {}).get("contract")
    )
    if not contract:
        return

    pending = load_pending_reviews()
    pending[contract] = checkpoint
    write_json(PENDING_REVIEW_FILE, pending)


def save_checkpoint(
    token: dict[str, Any],
    launch: dict[str, Any],
    holders_checked: list[dict[str, Any]],
    qualifying_holders: list[dict[str, Any]],
) -> None:
    write_json(
        CHECKPOINT_FILE,
        {
            "updated_at_utc": utc_now(),
            "token": token,
            "launch": launch,
            "holders_checked": holders_checked,
            "qualifying_holders": qualifying_holders,
            "api_failure_count": API_FAILURE_COUNT,
            "api_failure_log": API_FAILURE_LOG[-100:],
        },
    )


def save_all_reports(
    reports: list[dict[str, Any]],
    scanner_token_count: int,
) -> None:
    existing_reports = load_existing_reports()

    combined_reports = merge_reports(
        existing_reports,
        reports,
    )

    existing_top = load_existing_top_results()

    cumulative_top = merge_top_results(
        existing_top,
        reports,
    )

    write_json(
        FULL_OUTPUT_FILE,
        {
            "generated_at_utc": utc_now(),
            "scanner_tokens_available": scanner_token_count,
            "tokens_completed": len(combined_reports),
            "reports": combined_reports,
            "failed_transaction_hashes": sorted(FAILED_TX_HASHES),
            "failed_internal_hashes": sorted(FAILED_INTERNAL_HASHES),
            "api_failure_count": API_FAILURE_COUNT,
            "api_failure_log": API_FAILURE_LOG[-500:],
        },
    )

    backup_top_database()

    write_json(
        TOP_OUTPUT_FILE,
        {
            "generated_at_utc": utc_now(),
            "tokens_completed": len(cumulative_top),
            "results": cumulative_top,
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

    contract = scanner_token["contract"]

    token_info = fetch_token_info(contract)

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

    # -----------------------------------------------------
    # A) CURRENT TOP 20 HOLDERS
    # -----------------------------------------------------

    top_holders = fetch_top_holders(
        contract,
        decimals,
    )

    if top_holders is None:
        print(
            "Current holder list could not be fetched. "
            "Leaving token pending."
        )
        return None

    if not top_holders:
        print(
            "No eligible current holders returned. "
            "Leaving token pending."
        )
        return None

    print(
        f"\n  Current eligible holders loaded: "
        f"{len(top_holders)}"
    )

    for holder in top_holders:
        print(
            f"  Holder #{holder['holder_rank']}: "
            f"{holder['wallet']} | "
            f"balance {format_number(holder['current_balance'])}"
        )

    # -----------------------------------------------------
    # B) TOKEN LAUNCH REFERENCE
    # -----------------------------------------------------

    token_rows = fetch_token_transfers(
        contract=contract,
        max_pages=MAX_TOKEN_PAGES_FOR_LAUNCH,
    )

    if token_rows is None:
        print(
            "Token launch history could not be fetched. "
            "Leaving token pending."
        )
        return None

    launch_buy = find_first_confirmed_buy(
        rows=token_rows,
        contract=contract,
        decimals=decimals,
    )

    if not launch_buy:
        print(
            "Could not establish first genuine DEX buy / launch reference. "
            "Leaving token pending."
        )
        return None

    launch_time = parse_time(
        launch_buy.get("entry_time_utc")
    )

    launch_block = safe_int(
        launch_buy.get("entry_block")
    )

    if launch_time is None or launch_block <= 0:
        print(
            "Launch reference is incomplete. "
            "Leaving token pending."
        )
        return None

    launch = {
        "first_confirmed_buy_wallet": launch_buy["wallet"],
        "first_confirmed_buy_transaction_hash": (
            launch_buy["transaction_hash"]
        ),
        "first_confirmed_buy_block": launch_block,
        "first_confirmed_buy_time_utc": (
            launch_buy["entry_time_utc"]
        ),
        "early_window_seconds": EARLY_WINDOW_SECONDS,
        "early_window": "30 minutes",
    }

    print(
        f"\n  Launch reference: "
        f"{launch['first_confirmed_buy_time_utc']} | "
        f"block {launch_block:,}"
    )

    print(
        "  Qualification: current Top-30 holder, verified profitable "
        "DEX trading position and at least 25% of purchased tokens retained."
    )

    # -----------------------------------------------------
    # C) ANALYSE EACH CURRENT TOP-30 HOLDER
    # -----------------------------------------------------

    snapshot_result = rpc_call("eth_blockNumber")

    if snapshot_result is None:
        print(
            "  Could not establish blockchain snapshot. "
            "Leaving token pending."
        )
        return None

    snapshot_block = safe_int(snapshot_result)

    if snapshot_block <= 0:
        print(
            "  Invalid blockchain snapshot block. "
            "Leaving token pending."
        )
        return None

    print(f"  Blockchain snapshot: block {snapshot_block:,}")

    holders_checked = []
    qualifying_holders = []

    token_summary = {
        "name": name,
        "symbol": symbol,
        "contract": contract,
        "decimals": decimals,
    }

    for holder_index, holder in enumerate(
        top_holders,
        start=1,
    ):
        wallet = holder["wallet"]

        print(
            f"\n  Analysing current holder "
            f"{holder_index}/{len(top_holders)}: "
            f"{wallet}"
        )

        holder_api_failure_start = API_FAILURE_COUNT

        wallet_rows = fetch_token_transfers(
            contract=contract,
            address=wallet,
            start_block=launch_block,
            max_pages=MAX_HOLDER_WALLET_PAGES,
            end_block=snapshot_block,
        )

        if wallet_rows is None:
            checked = {
                **holder,
                "analysis_complete": False,
                "qualifies_30_minute_filter": False,
                "reason": "Wallet token history API failure",
            }

            holders_checked.append(checked)

            save_checkpoint(
                token=token_summary,
                launch=launch,
                holders_checked=holders_checked,
                qualifying_holders=qualifying_holders,
            )

            continue

        first_buy = find_holder_first_buy(
            wallet=wallet,
            rows=wallet_rows,
            contract=contract,
            decimals=decimals,
        )

        entry_time = (
            parse_time(first_buy.get("entry_time_utc"))
            if first_buy else None
        )

        # If no DEX buy was detected, use the earliest inbound
        # token transfer as a provisional acquisition reference.
        acquisition_method = (
            "DEX BUY CONFIRMED" if first_buy
            else "DEX BUY NOT CONFIRMED"
        )

        if not first_buy:
            print(
                "    No confirmed DEX buy — continuing with "
                "full holder position analysis."
            )

        seconds_after_launch = (
            int((entry_time - launch_time).total_seconds())
            if entry_time is not None else None
        )

        qualifies_window = (
            seconds_after_launch is not None
            and 0 <= seconds_after_launch <= EARLY_WINDOW_SECONDS
        )

        checked = {
            **holder,
            **(first_buy or {}),
            "acquisition_method": acquisition_method,
            "seconds_after_launch": seconds_after_launch,
            "delay_after_launch": format_duration(
                seconds_after_launch
            ),
            "qualifies_30_minute_filter": qualifies_window,
            "analysis_complete": (
                API_FAILURE_COUNT == holder_api_failure_start
            ),
        }

        whale_holdings = calculate_whale_holdings(
            wallet=wallet,
            wallet_rows=wallet_rows,
            decimals=decimals,
            current_balance=holder["current_balance"],
        )
        checked["whale_holdings"] = whale_holdings

        # Use the first observed inbound transfer when a DEX buy
        # has not been confirmed.
        if not first_buy:
            first_block = whale_holdings.get(
                "first_acquisition_block"
            )

            first_time = (
                parse_time(rpc_block_timestamp(first_block))
                if first_block and first_block > 0
                else None
            )

            acquisition_delay = (
                int((first_time - launch_time).total_seconds())
                if first_time is not None
                else None
            )

            checked["entry_time_utc"] = (
                first_time.isoformat()
                if first_time is not None else None
            )
            checked["seconds_after_launch"] = acquisition_delay
            checked["delay_after_launch"] = format_duration(
                acquisition_delay
            )
            checked["acquisition_method"] = (
                "FIRST OBSERVED TOKEN INBOUND"
            )

        # Entry timing is descriptive, not an exclusion criterion.
        # All verified genuine buyers proceed to sale analysis.
        # Full-position reconstruction is the authoritative qualification
        # method. Do not run the legacy first-buy sale analysis.
        qualified = {
            **checked,
            "classification": "HOLDER UNDER EVALUATION",
            "reason": (
                "Current major holder; acquisition method recorded separately; "
                "full-position profitability verification pending"
            ),
        }

        position = reconstruct_holder_position(
            wallet=wallet,
            wallet_rows=wallet_rows,
            contract=contract,
            decimals=decimals,
            current_balance=holder["current_balance"],
            snapshot_block=snapshot_block,
            history_start_block=launch_block,
        )

        qualified["full_position_analysis"] = position

        position_status = position.get("position_status")

        qualified["analysis_complete"] = (
            checked.get("analysis_complete") is True
            and position_status in {"VERIFIED", "UNVERIFIED"}
        )

        if position_status == "INCOMPLETE":
            qualified["classification"] = "ANALYSIS INCOMPLETE"
        elif position_status == "UNVERIFIED":
            qualified["classification"] = "PROFITABILITY UNVERIFIED"
        elif position_status == "VERIFIED":
            bought = safe_float(position.get("total_tokens_bought"))
            retained = safe_float(position.get("reconstructed_tokens_held"))
            retained_pct = (
                retained / bought * 100.0
                if bought > 0 else 0.0
            )
            qualified["retained_purchase_percentage"] = retained_pct

            profitable = (
                safe_float(position.get("realised_profit_eth")) > 0
                and safe_int(position.get("sell_count")) > 0
                and retained_pct >= 25.0
                and qualified.get("analysis_complete") is True
                and position_status == "VERIFIED"
            )

            if profitable:
                qualified["classification"] = "VERIFIED PROFITABLE CONVICTION HOLDER"
                qualified["reason"] = (
                    "Verified positive realised ETH profit with at least "
                    "25% of purchased tokens retained"
                )
                qualifying_holders.append(qualified)
            else:
                qualified["classification"] = "VERIFIED - BELOW CONVICTION THRESHOLD"

        holders_checked.append(qualified)

        print(
            f"    {qualified['classification']} | "
            f"holder #{holder['holder_rank']} | "
            f"entry {qualified.get('delay_after_launch')}"
        )

        if position_status == "VERIFIED":
            print(
                "    Full-position realised profit: "
                f"{format_number(position.get('realised_profit_eth'))} ETH | "
                "ROI "
                f"{format_number(position.get('realised_roi_percent'))}% | "
                "retained "
                f"{format_number(qualified.get('retained_purchase_percentage'))}%"
            )
        else:
            print(
                "    Position verification: "
                f"{position_status} | "
                f"{position.get('reason', 'Unknown reason')}"
            )

        save_checkpoint(
            token=token_summary,
            launch=launch,
            holders_checked=holders_checked,
            qualifying_holders=qualifying_holders,
        )

    # -----------------------------------------------------
    # D) SAFETY — NEVER MARK API-DAMAGED TOKEN COMPLETE
    # -----------------------------------------------------

    incomplete_holders = [
        holder
        for holder in holders_checked
        if holder.get("analysis_complete") is not True
    ]

    token_api_failures = (
        API_FAILURE_COUNT - token_api_failure_start
    )

    if incomplete_holders or token_api_failures:
        print(
            "\n  Token analysis incomplete: "
            f"{len(incomplete_holders)} holder(s) incomplete; "
            f"{token_api_failures} API failure(s)."
        )

        print(
            "  Checkpoint saved. Token will NOT be marked completed "
            "and will be retried next run."
        )

        return None

    # -----------------------------------------------------
    # E) RANK VERIFIED PROFITABLE CURRENT HOLDERS
    # -----------------------------------------------------

    qualifying_holders.sort(
        key=lambda holder: (
            -safe_float(
                holder.get("full_position_analysis", {}).get(
                    "realised_roi_percent"
                )
            ),
            -safe_float(
                holder.get("full_position_analysis", {}).get(
                    "realised_profit_eth"
                )
            ),
            -safe_float(
                holder.get("retained_purchase_percentage")
            ),
            safe_int(
                holder.get("holder_rank"),
                999_999,
            ),
        )
    )

    for position, holder in enumerate(
        qualifying_holders,
        start=1,
    ):
        holder["conviction_entry_rank"] = position

    top_conviction_holders = qualifying_holders[
        :TOP_CONVICTION_PER_TOKEN
    ]

    print("\n  TOP VERIFIED PROFITABLE CONVICTION HOLDERS")
    print("  " + "-" * 94)

    if not top_conviction_holders:
        print(
            "  No current Top-30 holder met the verified profitability "
            "and 25% retained-holdings requirements."
        )

    for holder in top_conviction_holders:
        position_data = holder["full_position_analysis"]

        print(
            f"  #{holder['conviction_entry_rank']} "
            f"{holder['wallet']} | "
            f"current holder #{holder['holder_rank']} | "
            f"realised profit "
            f"{format_number(position_data['realised_profit_eth'])} ETH | "
            f"ROI "
            f"{format_number(position_data['realised_roi_percent'])}% | "
            f"retained "
            f"{format_number(holder['retained_purchase_percentage'])}%"
        )

    return {
        "token": {
            **token_summary,
            "scanner_data": scanner_token,
        },
        "launch": launch,
        "token_analysis_complete": True,
        "analysis_version": ANALYSIS_VERSION,
        "analysis_method": (
            "Current Top 30 holders -> genuine DEX trading history -> "
            "complete ETH-denominated position reconstruction -> "
            "positive realised profit and minimum 25% retained -> "
            "rank by realised ROI, ETH profit and retained percentage"
        ),
        "settings": {
            "top_holder_limit": TOP_HOLDER_LIMIT,
            "early_window_seconds": EARLY_WINDOW_SECONDS,
            "early_window_minutes": 30,
            "early_window_is_qualification_filter": False,
            "top_conviction_per_token": TOP_CONVICTION_PER_TOKEN,
            "minimum_qualifying_wallets": 0,
            "profit_required_to_qualify": True,
            "sale_required_to_qualify": True,
            "minimum_realised_profit_eth_exclusive": 0.0,
            "minimum_retained_purchase_percentage": 25.0,
            "profit_calculation": "weighted_average_cost_eth",
            "ranking_priority": [
                "realised_roi_percent",
                "realised_profit_eth",
                "retained_purchase_percentage",
            ],
        },
        "current_top_holders": top_holders,
        "holders_checked": holders_checked,
        "qualifying_holder_count": len(qualifying_holders),
        "qualifying_holders": qualifying_holders,
        "top_conviction_holders": top_conviction_holders,
        "api_failures_during_token": 0,
    }


# =========================================================
# MAIN
# =========================================================

def main() -> None:
    if not BLOCKSCOUT_API_KEY:
        print(
            "BLOCKSCOUT_API_KEY is not loaded. "
            "Add it as a GitHub Codespaces secret, restart the "
            "Codespace, and run this analyser again."
        )
        return

    print(
        "Blockscout PRO API authenticated for "
        f"Robinhood Chain ID {CHAIN_ID}."
    )

    load_persistent_api_caches()

    scanner_tokens = load_scanner_tokens()

    print(
        f"Stage 1 tokens available for conviction analysis: "
        f"{len(scanner_tokens)}"
    )

    if not scanner_tokens:
        print(
            f"No scanner tokens found in {INPUT_FILE}."
        )
        return

    existing_reports = load_existing_reports()

    completed_contracts = {
        report_contract(report)
        for report in existing_reports
        if (
            report_is_complete(report)
            and report.get("analysis_version", 1) >= ANALYSIS_VERSION
        )
    }

    completed_contracts.discard("")

    if completed_contracts:
        print(
            f"Previously completed conviction tokens: "
            f"{len(completed_contracts)}"
        )

    pending_reviews = load_pending_reviews()
    pending_tokens = [
        token
        for token in scanner_tokens
        if token["contract"] not in completed_contracts
        and normalise_address(token["contract"]) not in pending_reviews
    ]

    if MAX_NEW_TOKENS_PER_RUN > 0:
        pending_tokens = pending_tokens[
            :MAX_NEW_TOKENS_PER_RUN
        ]

    print(
        f"New tokens to analyse: "
        f"{len(pending_tokens)}"
    )

    if not pending_tokens:
        print(
            "Nothing new to analyse. Existing conviction-holder "
            "database has been preserved."
        )
        return

    new_reports = []

    try:
        for index, token in enumerate(
            pending_tokens,
            start=1,
        ):
            report = analyse_token(
                scanner_token=token,
                run_index=index,
                run_total=len(pending_tokens),
            )

            if report is not None:
                new_reports.append(report)

                # Save immediately after EACH successfully completed token.
                # A crash/restart cannot wipe prior successful work.
                save_all_reports(
                    reports=new_reports,
                    scanner_token_count=len(scanner_tokens),
                )

                print(
                    "\n  Token completed and saved immediately."
                )

            else:
                save_pending_review()
                print(
                    "\n  Token saved for pending review; "
                    "the next run will advance to another token."
                )

            save_persistent_api_caches()

            if index < len(pending_tokens):
                time.sleep(TOKEN_DELAY)

    except KeyboardInterrupt:
        if new_reports:
            save_all_reports(
                reports=new_reports,
                scanner_token_count=len(scanner_tokens),
            )

        save_persistent_api_caches()

        print(
            "\nConviction-holder analyser stopped safely. "
            "Completed token results and conviction-only caches were preserved."
        )
        return

    save_persistent_api_caches()

    print("\n" + "=" * 100)
    print("CONVICTION HOLDER ANALYSIS COMPLETE")
    print("=" * 100)

    print(
        f"Successful new token reports this run: "
        f"{len(new_reports)}"
    )

    print(
        f"Full results saved to: "
        f"{FULL_OUTPUT_FILE}"
    )

    print(
        f"Live top conviction wallets saved to: "
        f"{TOP_OUTPUT_FILE}"
    )

    print(
        f"Checkpoint saved to: "
        f"{CHECKPOINT_FILE}"
    )


if __name__ == "__main__":
    main()