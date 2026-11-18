import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# =========================================================
# SETTINGS
# =========================================================

RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
CHAIN_ID = 4663

POLL_SECONDS = 120

MAX_RETRIES = 5
RETRY_DELAYS = (5, 10, 20, 60)

# eth_getLogs is queried in manageable block ranges.
LOG_BLOCK_CHUNK = 2_000

STATE_FILE = Path("smart_wallet_rpc_tracker_state.json")
EVENTS_FILE = Path("smart_wallet_buy_events.jsonl")

# Telegram credentials are securely loaded from Codespaces secrets.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TELEGRAM_MAX_RETRIES = 4
TELEGRAM_RETRY_DELAYS = (3, 6, 12, 30)


# =========================================================
# TRACKED WALLET LOADING
# =========================================================

# These are the only wallets kept permanently inside this tracker.
# Every other wallet is loaded from top_early_traders.json each time
# the tracker starts.
SEED_WALLETS = {
    "Source Wallet 1": {
        "address": "0xa2ab5d98553665a10f47d6e6a03d0c0634cdd681",
        "stars": 4,
        "tier": "HIGH CONFIDENCE",
        "source": "Original verified source",
        "role": "Verified early source buyer",
        "historical_profit_eth": None,
        "historical_roi_percent": None,
        "historical_entry": "Verified early source",
        "historical_exit": None,
    },
    "Source Wallet 2": {
        "address": "0xb8f8bc74215acae40aaac6d0b1621a72ffda8f44",
        "stars": 4,
        "tier": "HIGH CONFIDENCE",
        "source": "Original verified source",
        "role": "Confirmed source buyer",
        "historical_profit_eth": None,
        "historical_roi_percent": None,
        "historical_entry": "Confirmed source buyer",
        "historical_exit": None,
    },
    "Source Wallet 3": {
        "address": "0x1710a4d6268edb6944c0f0f3a7d07d06313b9091",
        "stars": 4,
        "tier": "HIGH CONFIDENCE",
        "source": "Original verified source",
        "role": "Verified early source buyer",
        "historical_profit_eth": None,
        "historical_roi_percent": None,
        "historical_entry": "Verified early source",
        "historical_exit": None,
    },
}

TOP_EARLY_TRADERS_FILE = (
    Path(__file__).resolve().parent / "top_early_traders.json"
)


def _extract_token_entries(raw_data):
    """
    Support either a direct list of token analysis entries or a dictionary
    containing that list under a common top-level key.
    """
    if isinstance(raw_data, list):
        return raw_data

    if not isinstance(raw_data, dict):
        return []

    for key in (
        "results",
        "tokens",
        "token_results",
        "analyses",
        "data",
    ):
        entries = raw_data.get(key)

        if isinstance(entries, list):
            return entries

    if isinstance(raw_data.get("token"), dict):
        return [raw_data]

    return []


def _trader_stars(trader):
    """
    Convert analyser output into the existing five-star alert display.

    This function performs its own numeric conversion because the wallet
    loader runs before the later general-helper functions are defined.
    """
    try:
        score = float(trader.get("trader_score"))
    except (TypeError, ValueError):
        score = None

    try:
        rank = int(trader.get("performance_rank"))
    except (TypeError, ValueError):
        rank = None

    if score is not None:
        if score >= 85:
            return 5

        if score >= 70:
            return 4

        return 3

    if rank == 1:
        return 5

    if rank is not None and rank <= 5:
        return 4

    return 3


def load_tracked_wallets():
    """
    Build the full tracker wallet list from:

        1. The three permanent seed wallets.
        2. Every unique wallet inside top_early_traders.json.

    No historical CashCat, Arrow, UP, Hoodrat, Wishbone or other discovered
    wallet is hard-coded here. Those wallets must come from the JSON file.

    Wallet addresses are deduplicated case-insensitively.
    """
    tracked_wallets = {
        name: dict(details)
        for name, details in SEED_WALLETS.items()
    }

    address_to_name = {
        details["address"].lower(): name
        for name, details in tracked_wallets.items()
    }

    if not TOP_EARLY_TRADERS_FILE.exists():
        print(
            f"{TOP_EARLY_TRADERS_FILE.name} was not found. "
            "Only the three permanent source wallets will be monitored."
        )

        return tracked_wallets

    try:
        with TOP_EARLY_TRADERS_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            raw_data = json.load(file)

    except (OSError, json.JSONDecodeError) as error:
        print(
            f"Could not read {TOP_EARLY_TRADERS_FILE.name}: {error}. "
            "Only the three permanent source wallets will be monitored."
        )

        return tracked_wallets

    token_entries = _extract_token_entries(raw_data)

    dynamic_wallets_added = 0
    duplicate_wallets_skipped = 0
    invalid_wallets_skipped = 0

    for token_entry in token_entries:
        if not isinstance(token_entry, dict):
            continue

        token = token_entry.get("token") or {}

        if not isinstance(token, dict):
            token = {}

        token_name = str(
            token.get("name")
            or "Unknown token"
        )

        token_symbol = str(
            token.get("symbol")
            or "UNKNOWN"
        )

        token_source = (
            f"{token_name} ({token_symbol})"
        )

        top_traders = (
            token_entry.get("top_traders")
            or []
        )

        if not isinstance(top_traders, list):
            continue

        for trader in top_traders:
            if not isinstance(trader, dict):
                continue

            # Explicit exclusion requested: WISHBONE Trader #4 only.
            try:
                excluded_performance_rank = int(
                    trader.get("performance_rank")
                )
            except (TypeError, ValueError):
                excluded_performance_rank = None

            if (
                token_symbol.upper() == "WISHBONE"
                and excluded_performance_rank == 4
            ):
                continue

            address = str(
                trader.get("wallet")
                or ""
            ).strip().lower()

            if not (
                address.startswith("0x")
                and len(address) == 42
            ):
                invalid_wallets_skipped += 1
                continue

            if address in address_to_name:
                duplicate_wallets_skipped += 1
                continue

            try:
                performance_rank = int(
                    trader.get("performance_rank")
                )
            except (TypeError, ValueError):
                performance_rank = None

            if performance_rank is not None:
                base_name = (
                    f"{token_symbol} Trader "
                    f"#{performance_rank}"
                )
            else:
                base_name = (
                    f"{token_symbol} Trader "
                    f"#{dynamic_wallets_added + 1}"
                )

            wallet_name = base_name
            suffix = 2

            while wallet_name in tracked_wallets:
                wallet_name = (
                    f"{base_name} ({suffix})"
                )
                suffix += 1

            historical_exit = (
                trader.get(
                    "time_to_final_counted_exit"
                )
                or trader.get(
                    "time_to_first_exit"
                )
            )

            tracked_wallets[wallet_name] = {
                "address": address,
                "stars": _trader_stars(trader),
                "tier": "DYNAMIC DISCOVERY",
                "source": token_source,
                "role": (
                    "Auto-discovered profitable "
                    "early trader"
                ),
                "historical_profit_eth": (
                    trader.get("realised_profit")
                ),
                "historical_roi_percent": (
                    trader.get("realised_roi")
                ),
                "historical_entry": (
                    trader.get("delay_after_launch")
                ),
                "historical_exit": historical_exit,
            }

            address_to_name[address] = (
                wallet_name
            )

            dynamic_wallets_added += 1

    print(
        "Wallet loading complete: "
        f"{len(SEED_WALLETS)} permanent + "
        f"{dynamic_wallets_added} from JSON = "
        f"{len(tracked_wallets)} total wallets."
    )

    if duplicate_wallets_skipped:
        print(
            f"Skipped {duplicate_wallets_skipped} duplicate "
            "JSON wallet entr"
            + (
                "y."
                if duplicate_wallets_skipped == 1
                else "ies."
            )
        )

    if invalid_wallets_skipped:
        print(
            f"Skipped {invalid_wallets_skipped} invalid "
            "JSON wallet entr"
            + (
                "y."
                if invalid_wallets_skipped == 1
                else "ies."
            )
        )


    # Additional conviction wallets.
    conviction_wallets = {
        "HOODINU Conviction Holder #2": {
            "address": "0x4883910391ecc4dd0786d6bdd4bfbc765ab00657",
            "stars": 5,
            "tier": "VERIFIED CONVICTION",
            "source": "HOODINU",
            "role": "Verified profitable early conviction holder",
            "historical_profit_eth": 0.6524216666803023,
            "historical_roi_percent": 824.3466055420622,
        },
        "BONER Conviction Whale #9": {
            "address": "0x50f27cdb650879a41fb07038bf2b818845c20e17",
            "stars": 0,
            "tier": "EARLY WHALE WATCHLIST",
            "source": "BONER",
            "role": "Early-entry whale; profitability unverified",
            "historical_profit_eth": None,
            "historical_roi_percent": None,
        },
    }

    existing_addresses = {
        details["address"].lower()
        for details in tracked_wallets.values()
    }

    for name, details in conviction_wallets.items():
        if details["address"].lower() not in existing_addresses:
            tracked_wallets[name] = details
            existing_addresses.add(details["address"].lower())


    # AUTO CONVICTION WALLET IMPORT
    # Automatically include eligible wallets discovered by the analyser.
    conviction_file = Path("top_conviction_holders.json")

    if conviction_file.exists():
        try:
            with conviction_file.open() as f:
                conviction_data = json.load(f)

            existing_addresses = {
                details["address"].lower()
                for details in tracked_wallets.values()
            }

            added = 0

            for report in conviction_data.get("results", []):
                token = report.get("token") or {}
                symbol = token.get("symbol") or "UNKNOWN"

                verified = report.get("top_conviction_holders") or []

                # Early whales are already evaluated by the analyser.
                watchlist = [
                    holder
                    for holder in (report.get("holders_checked") or [])
                    if holder.get("classification") == "PROFITABILITY UNVERIFIED"
                    and holder.get("analysis_complete") is True
                    and holder.get("delay_after_launch") is not None
                ]

                for holder in verified + watchlist:
                    address = holder.get("wallet", "").lower()

                    if not address.startswith("0x") or len(address) != 42:
                        continue

                    if address in existing_addresses:
                        continue

                    is_verified = holder in verified

                    name = (
                        f"{symbol} "
                        f"{'Conviction' if is_verified else 'Early Whale'} "
                        f"#{holder.get('holder_rank', '?')}"
                    )

                    position = holder.get("full_position_analysis") or {}

                    tracked_wallets[name] = {
                        "address": address,
                        "stars": 5 if is_verified else 0,
                        "tier": (
                            "VERIFIED CONVICTION"
                            if is_verified else "EARLY WHALE WATCHLIST"
                        ),
                        "source": symbol,
                        "role": holder.get("reason") or "Conviction analyser discovery",
                        "historical_profit_eth": (
                            position.get("realised_profit_eth")
                            if is_verified else None
                        ),
                        "historical_roi_percent": (
                            position.get("realised_roi_percent")
                            if is_verified else None
                        ),
                    }

                    existing_addresses.add(address)
                    added += 1

            print(f"Automatic conviction wallet import: {added} new wallets")

        except (OSError, ValueError, TypeError) as exc:
            print(f"Conviction wallet import warning: {exc}")

    return tracked_wallets



def refresh_conviction_wallets():
    global TRACKED_WALLETS, TRACKED_ADDRESS_SET

    updated = load_tracked_wallets()
    known = {v["address"].lower() for v in TRACKED_WALLETS.values()}
    added = 0

    for name, details in updated.items():
        address = details["address"].lower()
        if address not in known:
            TRACKED_WALLETS[name] = details
            known.add(address)
            added += 1

    TRACKED_ADDRESS_SET = known
    if added:
        print(f"Automatically added {added} wallets")
    return added


TRACKED_WALLETS = load_tracked_wallets()


# ERC-20 Transfer(address,address,uint256) event topic.
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa"
    "952ba7f163c4a11628f55a4df523b3ef"
)

# Common DEX/router function selectors.
# The first four bytes of transaction input identify the function.
SWAP_SELECTORS = {
    "0x7ff36ab5": "swapExactETHForTokens",
    "0xfb3bdb41": "swapETHForExactTokens",
    "0x38ed1739": "swapExactTokensForTokens",
    "0x8803dbee": "swapTokensForExactTokens",
    "0x18cbafe5": "swapExactTokensForETH",
    "0x4a25d94a": "swapTokensForExactETH",
    "0x414bf389": "exactInputSingle",
    "0xc04b8d59": "exactInput",
    "0xdb3e2198": "exactOutputSingle",
    "0xf28c0498": "exactOutput",
    "0x3593564c": "execute",
    "0x24856bc3": "execute",
    "0xac9650d8": "multicall",
    "0x5ae401dc": "multicall",
}

# These may be used as the payment side of a swap.
# They are excluded only when they are the token received by the wallet,
# so USDC-funded purchases of other tokens will still trigger alerts.
EXCLUDED_SYMBOLS = {
    "USDC",
    "USDT",
    "DAI",
    "WETH",
    "ETH",
    "USDE",
    "USDG",
    "SYRUPUSDG",
}

TRACKED_ADDRESS_SET = {
    details["address"].lower()
    for details in TRACKED_WALLETS.values()
}

# Token information is stored here during runtime so repeated
# eth_call requests are avoided.
TOKEN_METADATA_CACHE = {}

# =========================================================
# GENERAL HELPERS
# =========================================================

def tracked_wallet_details(address):
    address = (address or "").lower()
    for name, details in TRACKED_WALLETS.items():
        if details["address"].lower() == address:
            return {"name": name, **details}
    return {"name":"Unknown tracked wallet","address":address,"stars":0,"tier":"UNKNOWN","source":"Unknown","role":"Unknown role","historical_profit_eth":None,"historical_roi_percent":None,"historical_entry":None,"historical_exit":None}

def tracked_wallet_name(address):
    return tracked_wallet_details(address)["name"]

def tracked_wallet_role(address):
    return tracked_wallet_details(address)["role"]

def tracked_wallet_stars(address):
    return tracked_wallet_details(address)["stars"]

def tracked_wallet_tier(address):
    return tracked_wallet_details(address)["tier"]

def safe_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def format_compact_usd(value):
    number = safe_number(value)
    if number is None: return "Unknown"
    absolute = abs(number)
    if absolute >= 1_000_000_000: return f"${number / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000: return f"${number / 1_000_000:.2f}M"
    if absolute >= 1_000: return f"${number / 1_000:.2f}K"
    return f"${number:,.2f}"

def format_usd_price(value):
    number = safe_number(value)
    if number is None: return "Unknown"
    if number == 0: return "$0"
    if abs(number) >= 1: return f"${number:,.6f}".rstrip("0").rstrip(".")
    return f"${number:.12f}".rstrip("0").rstrip(".")

def format_eth(value):
    number = safe_number(value)
    return "Not recorded" if number is None else f"{number:,.6f}".rstrip("0").rstrip(".") + " ETH"

def format_roi(value):
    number = safe_number(value)
    return "Not recorded" if number is None else f"{number:,.2f}%"

def star_string(stars):
    try: count = max(0, min(int(stars), 5))
    except (TypeError, ValueError): count = 0
    return "\u2b50" * count

def to_hex(number):
    return hex(int(number))

def from_hex(value, default=0):
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return default

def address_to_topic(address):
    """
    Convert an EVM address into a 32-byte indexed event topic.
    """

    clean_address = address.lower().replace("0x", "")

    return "0x" + clean_address.rjust(64, "0")

def topic_to_address(topic):
    """
    Extract the final 20 bytes of an indexed address topic.
    """

    if not isinstance(topic, str) or len(topic) < 42:
        return ""

    return "0x" + topic[-40:].lower()

def current_utc():
    return datetime.now(timezone.utc).isoformat()

def shorten_address(address):
    if not address or len(address) < 14:
        return address or "Unknown"

    return f"{address[:8]}...{address[-6:]}"

def format_display_amount(amount):
    if amount is None:
        return "Unknown"

    try:
        if amount >= 1_000_000:
            return f"{amount:,.2f}"

        if amount >= 1:
            return f"{amount:,.6f}".rstrip("0").rstrip(".")

        return f"{amount:.12f}".rstrip("0").rstrip(".")

    except (TypeError, ValueError):
        return str(amount)

# =========================================================
# RPC REQUESTS
# =========================================================

def rpc_call(method, params=None):
    """
    Make a JSON-RPC request with exponential backoff.

    Returns None after all retries fail, allowing the tracker
    to wait and safely try again later.
    """

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or [],
    }

    for attempt_index in range(MAX_RETRIES):
        attempt_number = attempt_index + 1
        wait_time = RETRY_DELAYS[
            min(attempt_index, len(RETRY_DELAYS) - 1)
        ]

        try:
            response = requests.post(
                RPC_URL,
                json=payload,
                timeout=40,
            )

            if response.status_code == 429:
                if attempt_number == MAX_RETRIES:
                    print(
                        "RPC rate limit continued after "
                        f"{MAX_RETRIES} attempts."
                    )
                    return None

                print(
                    "RPC rate limit reached "
                    f"({attempt_number}/{MAX_RETRIES}). "
                    f"Waiting {wait_time} seconds..."
                )
                time.sleep(wait_time)
                continue

            if response.status_code >= 500:
                if attempt_number == MAX_RETRIES:
                    print(
                        f"RPC server error {response.status_code} "
                        f"continued after {MAX_RETRIES} attempts."
                    )
                    return None

                print(
                    f"RPC server error {response.status_code} "
                    f"({attempt_number}/{MAX_RETRIES}). "
                    f"Waiting {wait_time} seconds..."
                )
                time.sleep(wait_time)
                continue

            response.raise_for_status()
            data = response.json()

            if "error" in data:
                error = data["error"]

                if attempt_number == MAX_RETRIES:
                    print(
                        f"RPC error after {MAX_RETRIES} attempts: "
                        f"{error}"
                    )
                    return None

                print(
                    f"RPC error ({attempt_number}/{MAX_RETRIES}): "
                    f"{error}\n"
                    f"Retrying in {wait_time} seconds..."
                )
                time.sleep(wait_time)
                continue

            return data.get("result")

        except (
            requests.RequestException,
            ValueError,
        ) as error:
            if attempt_number == MAX_RETRIES:
                print(
                    f"RPC request failed after "
                    f"{MAX_RETRIES} attempts: {error}"
                )
                return None

            print(
                f"RPC request error "
                f"({attempt_number}/{MAX_RETRIES}): {error}\n"
                f"Retrying in {wait_time} seconds..."
            )
            time.sleep(wait_time)

    return None

def get_latest_block_number():
    result = rpc_call("eth_blockNumber")

    if result is None:
        return None

    return from_hex(result)

def get_transfer_logs(from_block, to_block):
    """
    Find incoming ERC-20 transfers to tracked wallets as discovery triggers.

    These logs do not prove a buy. Each matching transaction is later fetched
    in full and classified from the wallet's net token flows.
    """

    recipient_topics = [
        address_to_topic(details["address"])
        for details in TRACKED_WALLETS.values()
    ]

    log_filter = {
        "fromBlock": to_hex(from_block),
        "toBlock": to_hex(to_block),
        "topics": [
            TRANSFER_TOPIC,
            None,
            recipient_topics,
        ],
    }

    result = rpc_call(
        "eth_getLogs",
        [log_filter],
    )

    if result is None:
        return None

    return result

def get_transaction(transaction_hash):
    return rpc_call(
        "eth_getTransactionByHash",
        [transaction_hash],
    )

def get_transaction_receipt(transaction_hash):
    """Return the full receipt, including every event log in the transaction."""
    return rpc_call(
        "eth_getTransactionReceipt",
        [transaction_hash],
    )

def get_block(block_number):
    return rpc_call(
        "eth_getBlockByNumber",
        [to_hex(block_number), False],
    )

def eth_call(contract, call_data):
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

# =========================================================
# TOKEN METADATA
# =========================================================

def decode_abi_string(raw_result):
    """
    Decode common ABI string responses.

    Handles normal dynamic strings and older bytes32-style symbols.
    """

    if not raw_result or raw_result == "0x":
        return None

    raw_hex = raw_result[2:]

    try:
        raw_bytes = bytes.fromhex(raw_hex)
    except ValueError:
        return None

    # Standard ABI dynamic string:
    # offset (32 bytes), length (32 bytes), data...
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

    # Some older tokens return bytes32 instead of string.
    try:
        return raw_bytes.rstrip(b"\x00").decode(
            "utf-8",
            errors="replace",
        )

    except UnicodeDecodeError:
        return None

def get_token_metadata(contract):
    contract = contract.lower()

    if contract in TOKEN_METADATA_CACHE:
        return TOKEN_METADATA_CACHE[contract]

    # name()
    name_result = eth_call(
        contract,
        "0x06fdde03",
    )

    # symbol()
    symbol_result = eth_call(
        contract,
        "0x95d89b41",
    )

    # decimals()
    decimals_result = eth_call(
        contract,
        "0x313ce567",
    )

    name = decode_abi_string(name_result) or "Unknown"
    symbol = decode_abi_string(symbol_result) or "UNKNOWN"

    decimals = 18

    if decimals_result and decimals_result != "0x":
        try:
            decimals = int(decimals_result, 16)

        except ValueError:
            decimals = 18

    metadata = {
        "name": name,
        "symbol": symbol,
        "decimals": decimals,
        "contract": contract,
    }

    TOKEN_METADATA_CACHE[contract] = metadata

    return metadata

def get_dexscreener_data(token_contract):
    url = (
        f"https://api.dexscreener.com/token-pairs/v1/"
        f"robinhood/{token_contract}"
    )

    try:
        response = requests.get(url, timeout=10)

        if response.status_code != 200:
            return {}

        pairs = response.json()

        if not pairs:
            return {}

        # Choose the pool with the highest liquidity.
        pair = max(
            pairs,
            key=lambda p: (
                p.get("liquidity", {})
                 .get("usd", 0)
            )
        )

        return {
            "market_cap": pair.get("marketCap"),
            "liquidity": pair.get("liquidity", {}).get("usd"),
            "price": pair.get("priceUsd"),
            "volume24h": pair.get("volume", {}).get("h24"),
            "dex_url": pair.get("url"),
            "website": (
                pair.get("info", {})
                    .get("websites", [{}])[0]
                    .get("url")
            ),
            "twitter": next(
                (
                    s.get("url")
                    for s in pair.get("info", {}).get("socials", [])
                    if s.get("platform") == "twitter"
                ),
                None,
            ),
            "telegram": next(
                (
                    s.get("url")
                    for s in pair.get("info", {}).get("socials", [])
                    if s.get("platform") == "telegram"
                ),
                None,
            ),
        }

    except Exception:
        return {}
# =========================================================
# STATE AND EVENT STORAGE
# =========================================================

def load_state():
    if not STATE_FILE.exists():
        return {
            "initialised": False,
            "last_rpc_block": None,
            "seen_buy_events": [],
        }

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            state = json.load(file)

    except (json.JSONDecodeError, OSError):
        print(
            "RPC tracker state could not be read. "
            "Starting with a fresh state."
        )

        return {
            "initialised": False,
            "last_rpc_block": None,
            "seen_buy_events": [],
        }

    state.setdefault("initialised", False)
    state.setdefault("last_rpc_block", None)
    state.setdefault("seen_buy_events", [])

    return state

def save_state(state):
    state["seen_buy_events"] = state[
        "seen_buy_events"
    ][-20_000:]

    with STATE_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            state,
            file,
            indent=4,
        )

def append_event(event):
    with EVENTS_FILE.open(
        "a",
        encoding="utf-8",
    ) as file:
        file.write(
            json.dumps(event) + "\n"
        )

def buy_event_identifier(event):
    return "|".join(
        [
            event["transaction_hash"],
            event["token_contract"],
            event["recipient_wallet"],
        ]
    )

# =========================================================
# BUY DETECTION
# =========================================================

def get_selector_and_method(transaction):
    input_data = str(
        transaction.get("input") or "0x"
    ).lower()

    selector = (
        input_data[:10]
        if len(input_data) >= 10
        else input_data
    )

    method = SWAP_SELECTORS.get(
        selector,
        f"Unknown selector {selector}",
    )

    return selector, method


def decode_transfer_log(log):
    """Decode a standard ERC-20 Transfer log."""
    topics = log.get("topics") or []

    if len(topics) < 3:
        return None

    if str(topics[0]).lower() != TRANSFER_TOPIC.lower():
        return None

    token_contract = str(log.get("address") or "").lower()
    sender = topic_to_address(topics[1])
    recipient = topic_to_address(topics[2])
    raw_amount = from_hex(log.get("data"), default=0)

    if not token_contract or not sender or not recipient:
        return None

    return {
        "token_contract": token_contract,
        "sender": sender,
        "recipient": recipient,
        "raw_amount": raw_amount,
    }


def aggregate_wallet_token_flows(receipt, wallet_address):
    """
    Calculate the wallet's net ERC-20 movement for the whole transaction.

    A positive net amount means the wallet gained the token.
    A negative net amount means the wallet spent/sold the token.
    """
    wallet_address = wallet_address.lower()
    flows = {}

    for log in receipt.get("logs") or []:
        transfer = decode_transfer_log(log)

        if transfer is None:
            continue

        token_contract = transfer["token_contract"]
        flow = flows.setdefault(
            token_contract,
            {
                "received_raw": 0,
                "sent_raw": 0,
                "net_raw": 0,
            },
        )

        if transfer["recipient"] == wallet_address:
            flow["received_raw"] += transfer["raw_amount"]

        if transfer["sender"] == wallet_address:
            flow["sent_raw"] += transfer["raw_amount"]

    for flow in flows.values():
        flow["net_raw"] = (
            flow["received_raw"] - flow["sent_raw"]
        )

    return flows


def raw_to_token_amount(raw_amount, decimals):
    try:
        return raw_amount / (10 ** int(decimals))
    except (ValueError, TypeError, OverflowError):
        return None


def create_buy_events_from_transaction(
    transaction,
    receipt,
    block_timestamp,
):
    """
    Classify a complete transaction using net token flows.

    A Telegram event is created only when the tracked wallet has a net gain
    in a non-payment token and spends ETH or has a net loss in another token.
    Sells, partial exits, refunds and quote-token receipts are ignored.
    """
    transaction_sender = str(
        transaction.get("from") or ""
    ).lower()

    if transaction_sender not in TRACKED_ADDRESS_SET:
        return []

    selector, method = get_selector_and_method(transaction)

    # Keep the existing protection against ordinary transfers/airdrops.
    if selector not in SWAP_SELECTORS:
        return []

    flows = aggregate_wallet_token_flows(
        receipt,
        transaction_sender,
    )

    if not flows:
        return []

    enriched_flows = {}

    for token_contract, flow in flows.items():
        metadata = get_token_metadata(token_contract)
        net_amount = raw_to_token_amount(
            flow["net_raw"],
            metadata["decimals"],
        )
        received_amount = raw_to_token_amount(
            flow["received_raw"],
            metadata["decimals"],
        )
        sent_amount = raw_to_token_amount(
            flow["sent_raw"],
            metadata["decimals"],
        )

        enriched_flows[token_contract] = {
            **flow,
            "metadata": metadata,
            "net_amount": net_amount,
            "received_amount": received_amount,
            "sent_amount": sent_amount,
        }

    negative_token_flows = [
        flow
        for flow in enriched_flows.values()
        if flow["net_raw"] < 0
    ]

    native_value_raw = from_hex(
        transaction.get("value"),
        default=0,
    )

    # A genuine acquisition should have something spent on the other side.
    has_payment_outflow = bool(
        native_value_raw > 0
        or negative_token_flows
    )

    if not has_payment_outflow:
        return []

    transaction_hash = str(
        transaction.get("hash")
        or receipt.get("transactionHash")
        or ""
    ).lower()

    block_number = from_hex(
        receipt.get("blockNumber")
        or transaction.get("blockNumber")
    )

    payment_assets = []

    if native_value_raw > 0:
        payment_assets.append({
            "symbol": "ETH",
            "amount": native_value_raw / 10 ** 18,
            "contract": None,
        })

    for token_contract, flow in enriched_flows.items():
        if flow["net_raw"] >= 0:
            continue

        payment_assets.append({
            "symbol": flow["metadata"]["symbol"],
            "amount": abs(flow["net_amount"])
            if flow["net_amount"] is not None
            else None,
            "contract": token_contract,
        })

    events = []

    for token_contract, flow in enriched_flows.items():
        metadata = flow["metadata"]

        # This is the critical direction check. The wallet must finish the
        # transaction with more of this token than it started with.
        if flow["net_raw"] <= 0:
            continue

        # Quote/payment assets received during a sell are never buy targets.
        if metadata["symbol"].upper() in EXCLUDED_SYMBOLS:
            continue

        dex = get_dexscreener_data(token_contract)

        events.append({
            "detected_at_utc": current_utc(),
            "classification": (
                "Confirmed tracked-wallet net token acquisition"
            ),
            "buyer_wallet_name": tracked_wallet_name(
                transaction_sender
            ),
            "buyer_wallet_role": tracked_wallet_role(
                transaction_sender
            ),
            "buyer_wallet_stars": tracked_wallet_stars(
                transaction_sender
            ),
            "buyer_wallet_tier": tracked_wallet_tier(
                transaction_sender
            ),
            "buyer_wallet_details": tracked_wallet_details(
                transaction_sender
            ),
            "buyer_wallet": transaction_sender,
            "recipient_wallet_name": tracked_wallet_name(
                transaction_sender
            ),
            "recipient_wallet_role": tracked_wallet_role(
                transaction_sender
            ),
            "recipient_wallet": transaction_sender,
            "token_name": metadata["name"],
            "token_symbol": metadata["symbol"],
            "token_contract": token_contract,
            # Net amount, not a single incoming transfer or router refund.
            "amount": flow["net_amount"],
            "gross_received_amount": flow["received_amount"],
            "gross_sent_amount": flow["sent_amount"],
            "payment_assets": payment_assets,
            "market_cap": dex.get("market_cap"),
            "liquidity": dex.get("liquidity"),
            "price_usd": dex.get("price"),
            "volume_24h": dex.get("volume24h"),
            "dex_url": dex.get("dex_url"),
            "website": dex.get("website"),
            "twitter": dex.get("twitter"),
            "telegram": dex.get("telegram"),
            "transaction_selector": selector,
            "transaction_method": method,
            "transaction_hash": transaction_hash,
            "block_number": block_number,
            "timestamp_utc": block_timestamp,
        })

    return events


def inspect_transfer_logs(
    logs,
    seen_buy_events,
):
    """
    Use incoming transfer logs only as transaction discovery triggers, then
    fetch each complete receipt and calculate net flows before alerting.
    """
    alerts = []
    transaction_hashes = []
    seen_hashes = set()

    for transfer_log in logs:
        transaction_hash = str(
            transfer_log.get("transactionHash") or ""
        ).lower()

        if transaction_hash and transaction_hash not in seen_hashes:
            seen_hashes.add(transaction_hash)
            transaction_hashes.append(transaction_hash)

    block_timestamps = {}

    for transaction_hash in transaction_hashes:
        transaction = get_transaction(transaction_hash)

        if transaction is None:
            continue

        transaction_sender = str(
            transaction.get("from") or ""
        ).lower()

        if transaction_sender not in TRACKED_ADDRESS_SET:
            continue

        receipt = get_transaction_receipt(transaction_hash)

        if receipt is None:
            continue

        # Failed/reverted transactions never count as buys.
        if from_hex(receipt.get("status"), default=0) != 1:
            continue

        block_number = from_hex(
            receipt.get("blockNumber")
            or transaction.get("blockNumber")
        )

        if block_number not in block_timestamps:
            block = get_block(block_number)

            if block is None:
                block_timestamps[block_number] = None
            else:
                timestamp = from_hex(block.get("timestamp"))
                block_timestamps[block_number] = (
                    datetime.fromtimestamp(
                        timestamp,
                        timezone.utc,
                    ).isoformat()
                )

        transaction_events = create_buy_events_from_transaction(
            transaction=transaction,
            receipt=receipt,
            block_timestamp=block_timestamps[block_number],
        )

        for event in transaction_events:
            identifier = buy_event_identifier(event)

            if identifier in seen_buy_events:
                continue

            seen_buy_events.add(identifier)
            alerts.append(event)

    return alerts

# =========================================================
# TERMINAL OUTPUT
# =========================================================

def print_buy_alert(event):
    details = event.get("buyer_wallet_details") or tracked_wallet_details(event.get("buyer_wallet"))
    stars = star_string(details.get("stars"))
    tier = details.get("tier") or "SMART WALLET"
    print("\n" + "=" * 90)
    print(f"{stars} {tier} SMART WALLET BUY")
    print("=" * 90)
    print(f"Buyer: {details.get('name')}")
    print(f"Source: {details.get('source')}")
    print(f"Role: {details.get('role')}")
    print(f"Buyer address: {event['buyer_wallet']}")
    if details.get("historical_profit_eth") is not None:
        print(f"Historical profit: {format_eth(details.get('historical_profit_eth'))}")
    if details.get("historical_roi_percent") is not None:
        print(f"Historical ROI: {format_roi(details.get('historical_roi_percent'))}")
    print(f"Token: {event['token_name']} ({event['token_symbol']})")
    print(f"Contract: {event['token_contract']}")
    print(f"Market cap: {format_compact_usd(event.get('market_cap'))}")
    print(f"Liquidity: {format_compact_usd(event.get('liquidity'))}")
    print(f"Price: {format_usd_price(event.get('price_usd'))}")
    print(f"24h volume: {format_compact_usd(event.get('volume_24h'))}")
    if event.get("amount") is not None:
        print(f"Amount received: {format_display_amount(event['amount'])} {event['token_symbol']}")
    print(f"Method: {event['transaction_method']}")
    print(f"Time: {event['timestamp_utc']}")
    print(f"Block: {event['block_number']}")
    print(f"Transaction: {event['transaction_hash']}")
    print("Blockscout: https://robinhoodchain.blockscout.com/tx/" + event["transaction_hash"])
    print("DexScreener: " + (event.get("dex_url") or "https://dexscreener.com/search?q=" + event["token_contract"]))
    print("=" * 90 + "\n")

# =========================================================
# TELEGRAM ALERTS
# =========================================================

def telegram_is_configured():
    return bool(
        TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
    )

def build_telegram_message(event):
    transaction_hash = event["transaction_hash"]
    token_contract = event["token_contract"]
    details = event.get("buyer_wallet_details") or tracked_wallet_details(event.get("buyer_wallet"))
    stars = star_string(details.get("stars"))
    tier = details.get("tier") or "SMART WALLET"
    blockscout_url = "https://robinhoodchain.blockscout.com/tx/" + transaction_hash
    market_cap = format_compact_usd(event.get("market_cap"))
    liquidity = format_compact_usd(event.get("liquidity"))
    price = format_usd_price(event.get("price_usd"))
    volume = format_compact_usd(event.get("volume_24h"))
    website = event.get("website") or "Not available"
    twitter = event.get("twitter") or "Not available"
    telegram = event.get("telegram") or "Not available"
    dexscreener_url = event.get("dex_url") or f"https://dexscreener.com/robinhood/{token_contract}"
    amount = format_display_amount(event.get("amount"))
    history_lines = []
    if details.get("historical_entry"): history_lines.append(f"Entry: {details['historical_entry']}")
    if details.get("historical_exit"): history_lines.append(f"Exit: {details['historical_exit']}")
    if details.get("historical_profit_eth") is not None: history_lines.append("Profit: " + format_eth(details["historical_profit_eth"]))
    if details.get("historical_roi_percent") is not None: history_lines.append("ROI: " + format_roi(details["historical_roi_percent"]))
    historical_performance = "\n".join(history_lines) if history_lines else "Verified source wallet"
    return (
        f"{stars} {tier} SMART WALLET BUY\n\n"
        "\U0001f464 Wallet\n" f"{details.get('name')}\n" f"{event['buyer_wallet']}\n\n"
        "\U0001f3f7 Source / Classification\n" f"{details.get('source')}\n" f"{details.get('role')}\n\n"
        "\U0001f4da Historical Performance\n" f"{historical_performance}\n\n"
        "\U0001fa99 New Token\n" f"{event['token_name']} ({event['token_symbol']})\n\n"
        "\U0001f4b0 Market Cap\n" f"{market_cap}\n\n"
        "\U0001f4a7 Liquidity\n" f"{liquidity}\n\n"
        "\U0001f4b5 Price\n" f"{price}\n\n"
        "\U0001f4ca 24h Volume\n" f"{volume}\n\n"
        "\U0001f4c8 Amount Bought\n" f"{amount} {event['token_symbol']}\n\n"
        "\U0001f4e6 Contract\n" f"{token_contract}\n\n"
        "\U0001f310 Website\n" f"{website}\n\n"
        "\U0001f426 X / Twitter\n" f"{twitter}\n\n"
        "\U0001f4ac Telegram\n" f"{telegram}\n\n"
        "\U0001f4c8 DexScreener\n" f"{dexscreener_url}\n\n"
        "\U0001f50e Blockscout\n" f"{blockscout_url}\n\n"
        "\U0001f552 Time\n" f"{event['timestamp_utc']}\n\n"
        "\U0001f9f1 Block\n" f"{event['block_number']}"
    )


def send_telegram_message(message):
    """
    Send one Telegram message.

    Telegram problems will not stop the blockchain tracker.
    """

    if not telegram_is_configured():
        print(
            "Telegram not configured: "
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing."
        )
        return False

    telegram_url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    for attempt_index in range(TELEGRAM_MAX_RETRIES):
        attempt_number = attempt_index + 1

        wait_time = TELEGRAM_RETRY_DELAYS[
            min(
                attempt_index,
                len(TELEGRAM_RETRY_DELAYS) - 1,
            )
        ]

        try:
            response = requests.post(
                telegram_url,
                json=payload,
                timeout=30,
            )

            if response.status_code == 429:
                retry_after = wait_time

                try:
                    response_data = response.json()

                    retry_after = int(
                        response_data
                        .get("parameters", {})
                        .get("retry_after", wait_time)
                    )

                except (ValueError, TypeError):
                    retry_after = wait_time

                if attempt_number == TELEGRAM_MAX_RETRIES:
                    print(
                        "Telegram remained rate limited. "
                        "Alert could not be delivered."
                    )
                    return False

                print(
                    "Telegram rate limit reached. "
                    f"Waiting {retry_after} seconds..."
                )

                time.sleep(retry_after)
                continue

            if response.status_code >= 500:
                if attempt_number == TELEGRAM_MAX_RETRIES:
                    print(
                        "Telegram remained unavailable. "
                        "Alert could not be delivered."
                    )
                    return False

                print(
                    f"Telegram server error "
                    f"{response.status_code}. "
                    f"Retrying in {wait_time} seconds..."
                )

                time.sleep(wait_time)
                continue

            response.raise_for_status()
            response_data = response.json()

            if not response_data.get("ok"):
                print(
                    "Telegram rejected the message:",
                    response_data,
                )
                return False

            print(
                "Telegram buy alert delivered successfully."
            )
            return True

        except (
            requests.RequestException,
            ValueError,
        ) as error:
            if attempt_number == TELEGRAM_MAX_RETRIES:
                print(
                    "Telegram alert failed after "
                    f"{TELEGRAM_MAX_RETRIES} attempts: "
                    f"{error}"
                )
                return False

            print(
                f"Telegram request error: {error}\n"
                f"Retrying in {wait_time} seconds..."
            )

            time.sleep(wait_time)

    return False

def send_telegram_alert(event):
    message = build_telegram_message(event)

    return send_telegram_message(message)

# =========================================================
# TRACKER
# =========================================================

def initialise_rpc_baseline(state):
    latest_block = get_latest_block_number()

    if latest_block is None:
        print(
            "Could not create the RPC baseline. "
            f"Retrying in {POLL_SECONDS} seconds..."
        )
        return False

    state["last_rpc_block"] = latest_block
    state["initialised"] = True
    state["baseline_completed_at_utc"] = (
        current_utc()
    )

    save_state(state)

    print("\nRPC baseline created successfully.")
    print(
        f"Starting from block: {latest_block:,}"
    )
    print(
        "Historical activity will not trigger alerts."
    )

    return True

def process_block_range(
    start_block,
    end_block,
    seen_buy_events,
):
    """
    Process a large range in smaller chunks.

    Returns:
        alerts,
        last_successfully_processed_block,
        success
    """

    alerts = []
    current_start = start_block
    last_processed = start_block - 1

    while current_start <= end_block:
        current_end = min(
            current_start + LOG_BLOCK_CHUNK - 1,
            end_block,
        )

        print(
            f"Scanning blocks "
            f"{current_start:,} Ã¢ÂÂ {current_end:,}..."
        )

        logs = get_transfer_logs(
            current_start,
            current_end,
        )

        if logs is None:
            print(
                "RPC log query failed. The last block will "
                "not be advanced, so this range will be retried."
            )

            return (
                alerts,
                last_processed,
                False,
            )

        if logs:
            new_alerts = inspect_transfer_logs(
                logs,
                seen_buy_events,
            )

            alerts.extend(new_alerts)

        last_processed = current_end
        current_start = current_end + 1

    return alerts, last_processed, True

def run_tracker():
    state = load_state()

    while not state.get("initialised"):
        if initialise_rpc_baseline(state):
            break

        time.sleep(POLL_SECONDS)
        state = load_state()

    seen_buy_events = set(
        state.get("seen_buy_events", [])
    )

    print(
        f"\nRPC tracker is monitoring "
        f"{len(TRACKED_WALLETS)} wallets."
    )
    print(
        f"Checking every {POLL_SECONDS} seconds."
    )
    print(
        f"Robinhood Chain ID: {CHAIN_ID}"
    )

    if telegram_is_configured():
        print(
            "Telegram alerts: connected."
        )
    else:
        print(
            "Telegram alerts: NOT configured."
        )

    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            # AUTO REFRESH IN MONITORING LOOP
            refresh_conviction_wallets()
            latest_block = get_latest_block_number()

            if latest_block is None:
                print(
                    "RPC unavailable. State has not advanced. "
                    f"Trying again in {POLL_SECONDS} seconds.\n"
                )

                time.sleep(POLL_SECONDS)
                continue

            last_block = int(
                state.get("last_rpc_block")
                or latest_block
            )

            start_block = last_block + 1

            if start_block > latest_block:
                print(
                    f"[{current_utc()}] "
                    f"Chain at block {latest_block:,} Ã¢ÂÂ "
                    "no new blocks to scan."
                )

                print(
                    f"Next check in {POLL_SECONDS} seconds.\n"
                )

                time.sleep(POLL_SECONDS)
                continue

            print(
                f"\n[{current_utc()}] "
                f"New block range: "
                f"{start_block:,} Ã¢ÂÂ {latest_block:,}"
            )

            (
                alerts,
                last_processed,
                successful,
            ) = process_block_range(
                start_block=start_block,
                end_block=latest_block,
                seen_buy_events=seen_buy_events,
            )

            for event in alerts:
                print_buy_alert(event)

                telegram_delivered = send_telegram_alert(
                    event
                )

                event["telegram_delivered"] = (
                    telegram_delivered
                )

                event["telegram_attempted_at_utc"] = (
                    current_utc()
                )

                append_event(event)

            if last_processed >= start_block:
                state["last_rpc_block"] = (
                    last_processed
                )

            state["seen_buy_events"] = list(
                seen_buy_events
            )
            state["last_check_utc"] = current_utc()
            state["last_scan_successful"] = successful

            save_state(state)

            if alerts:
                print(
                    f"{len(alerts)} confirmed "
                    "smart-wallet buy alert(s)."
                )

            elif successful:
                print(
                    "Block range scanned successfully Ã¢ÂÂ "
                    "no confirmed tracked-wallet buys."
                )

            else:
                print(
                    "Scan was interrupted. The unprocessed "
                    "range will be retried next cycle."
                )

            print(
                f"Last processed block: "
                f"{state['last_rpc_block']:,}"
            )
            print(
                f"Next check in {POLL_SECONDS} seconds.\n"
            )

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        state["seen_buy_events"] = list(
            seen_buy_events
        )
        state["stopped_at_utc"] = current_utc()

        save_state(state)

        print(
            "\nRPC smart-wallet tracker stopped safely."
        )

if __name__ == "__main__":
    run_tracker()