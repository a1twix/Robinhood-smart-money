import json
import time
from collections import defaultdict

import requests


# =========================================================
# SETTINGS
# =========================================================

TOKENS_URL = (
    "https://robinhoodchain.blockscout.com/api/v2/tokens"
)

DEXSCREENER_BASE_URL = (
    "https://api.dexscreener.com/token-pairs/v1"
)

DEXSCREENER_CHAIN_ID = "robinhood"


# Original successful-token strategy.
MARKET_CAP_LIMIT = 5_000_000

# Activity filters to remove dead or misleading tokens.
MIN_LIQUIDITY_USD = 50_000
MIN_VOLUME_24H_USD = 10_000
MIN_TRANSACTIONS_24H = 20

# 24-hour volume must be at least 0.5% of liquidity.
MIN_VOLUME_LIQUIDITY_RATIO = 0.005


MAX_TOKEN_PAGES = 20
MAX_RETRIES = 5

REQUEST_TIMEOUT = 30
REQUEST_DELAY = 2
DEXSCREENER_DELAY = 0.35

RETRY_DELAYS = (5, 10, 20, 40, 60)


# Output files.
WALLETS_FILE = "wallets.json"
SMART_CANDIDATES_FILE = "smart_wallet_candidates.json"
ACTIVE_TOKENS_FILE = "active_successful_tokens.json"
REJECTED_TOKENS_FILE = "rejected_inactive_tokens.json"


# Do not use payment assets, stablecoins or wrapped assets
# to identify smart traders.
EXCLUDED_SYMBOLS = {
    "USDE",
    "USDG",
    "USDC",
    "USDT",
    "DAI",
    "WETH",
    "WBTC",
    "ETH",
    "SYRUPUSDG",
}


ZERO_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}


# =========================================================
# GENERAL HELPERS
# =========================================================

def safe_float(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def format_usd(value):
    number = safe_float(value)

    if number >= 1_000_000_000:
        return f"${number / 1_000_000_000:.2f}B"

    if number >= 1_000_000:
        return f"${number / 1_000_000:.2f}M"

    if number >= 1_000:
        return f"${number / 1_000:.2f}K"

    return f"${number:,.2f}"


def clean_params(params):
    """
    Convert Python booleans into API-compatible lowercase strings.
    """

    return {
        key: (
            str(value).lower()
            if isinstance(value, bool)
            else value
        )
        for key, value in params.items()
    }


def get_json(
    url,
    params=None,
    description="Request",
):
    """
    Request JSON with retries, rate-limit handling and safe failure.
    """

    for attempt in range(1, MAX_RETRIES + 1):
        wait_time = RETRY_DELAYS[
            min(
                attempt - 1,
                len(RETRY_DELAYS) - 1,
            )
        ]

        try:
            response = requests.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 429:
                retry_after = safe_int(
                    response.headers.get("Retry-After"),
                    wait_time,
                )

                if attempt == MAX_RETRIES:
                    print(
                        f"{description} remained rate limited "
                        f"after {MAX_RETRIES} attempts."
                    )
                    return None

                print(
                    f"{description} rate limited. "
                    f"Waiting {retry_after} seconds "
                    f"({attempt}/{MAX_RETRIES})..."
                )

                time.sleep(retry_after)
                continue

            if response.status_code >= 500:
                if attempt == MAX_RETRIES:
                    print(
                        f"{description} returned server error "
                        f"{response.status_code} after "
                        f"{MAX_RETRIES} attempts."
                    )
                    return None

                print(
                    f"{description} server error "
                    f"{response.status_code}. "
                    f"Retrying in {wait_time} seconds..."
                )

                time.sleep(wait_time)
                continue

            response.raise_for_status()
            return response.json()

        except (
            requests.RequestException,
            ValueError,
        ) as error:
            if attempt == MAX_RETRIES:
                print(
                    f"{description} failed after "
                    f"{MAX_RETRIES} attempts: {error}"
                )
                return None

            print(
                f"{description} error: {error}. "
                f"Retrying in {wait_time} seconds..."
            )

            time.sleep(wait_time)

    return None


# =========================================================
# BLOCKSCOUT TOKEN COLLECTION
# =========================================================

def fetch_blockscout_candidates():
    """
    Fetch and deduplicate Robinhood Chain tokens.

    The first stage retains the existing strategy:
    only tokens with a Blockscout circulating market cap
    of at least $5 million are shortlisted.
    """

    all_tokens = {}
    params = {}
    seen_cursors = set()

    print("Fetching Robinhood Chain tokens...\n")

    for page_number in range(
        1,
        MAX_TOKEN_PAGES + 1,
    ):
        data = get_json(
            TOKENS_URL,
            params=params,
            description=(
                f"Blockscout token page {page_number}"
            ),
        )

        if not isinstance(data, dict):
            print(
                "Blockscout token data was unavailable. "
                "Stopping token collection safely."
            )
            break

        items = data.get("items") or []

        for token in items:
            contract = token.get("address_hash")

            if contract:
                all_tokens[
                    contract.lower()
                ] = token

        print(
            f"Token page {page_number}: "
            f"{len(items)} returned, "
            f"{len(all_tokens)} unique tokens collected"
        )

        next_page = data.get("next_page_params")

        if not next_page:
            print("Reached the final token page.")
            break

        next_params = clean_params(next_page)

        cursor = tuple(
            sorted(
                (key, str(value))
                for key, value
                in next_params.items()
            )
        )

        if cursor in seen_cursors:
            print(
                "Blockscout repeated the token cursor. "
                "Stopping safely."
            )
            break

        seen_cursors.add(cursor)
        params = next_params

        time.sleep(REQUEST_DELAY)

    candidates = []

    for token in all_tokens.values():
        market_cap = safe_float(
            token.get("circulating_market_cap")
        )

        symbol = str(
            token.get("symbol") or ""
        ).upper()

        if market_cap < MARKET_CAP_LIMIT:
            continue

        if symbol in EXCLUDED_SYMBOLS:
            continue

        candidates.append(token)

    candidates.sort(
        key=lambda token: safe_float(
            token.get("circulating_market_cap")
        ),
        reverse=True,
    )

    print(
        f"\nFound {len(candidates)} Blockscout tokens "
        f"above {format_usd(MARKET_CAP_LIMIT)}.\n"
    )

    return candidates


# =========================================================
# DEXSCREENER ACTIVITY VALIDATION
# =========================================================

def fetch_dexscreener_pairs(token_contract):
    """
    Fetch all DexScreener pools for one Robinhood Chain token.
    """

    contract = str(
        token_contract or ""
    ).lower()

    if not contract:
        return []

    url = (
        f"{DEXSCREENER_BASE_URL}/"
        f"{DEXSCREENER_CHAIN_ID}/"
        f"{contract}"
    )

    data = get_json(
        url,
        description=(
            f"DexScreener {contract[:10]}..."
        ),
    )

    if not isinstance(data, list):
        return []

    return [
        pair
        for pair in data
        if isinstance(pair, dict)
        and str(
            pair.get("chainId") or ""
        ).lower() == DEXSCREENER_CHAIN_ID
    ]


def select_best_pair(pairs):
    """
    Choose the main pool by selecting the pair
    with the highest USD liquidity.
    """

    if not pairs:
        return None

    return max(
        pairs,
        key=lambda pair: safe_float(
            (pair.get("liquidity") or {}).get("usd")
        ),
    )


def extract_pair_metrics(pair):
    liquidity = safe_float(
        (pair.get("liquidity") or {}).get("usd")
    )

    volume_24h = safe_float(
        (pair.get("volume") or {}).get("h24")
    )

    transaction_data = (
        (pair.get("txns") or {}).get("h24")
        or {}
    )

    buys_24h = safe_int(
        transaction_data.get("buys")
    )

    sells_24h = safe_int(
        transaction_data.get("sells")
    )

    transactions_24h = (
        buys_24h + sells_24h
    )

    market_cap = safe_float(
        pair.get("marketCap")
    )

    fdv = safe_float(
        pair.get("fdv")
    )

    if market_cap > 0:
        effective_market_cap = market_cap
        valuation_type = "Market cap"
    else:
        effective_market_cap = fdv
        valuation_type = "FDV"

    if liquidity > 0:
        volume_liquidity_ratio = (
            volume_24h / liquidity
        )
    else:
        volume_liquidity_ratio = 0.0

    return {
        "market_cap": market_cap,
        "fdv": fdv,
        "effective_market_cap": effective_market_cap,
        "valuation_type": valuation_type,
        "liquidity": liquidity,
        "volume_24h": volume_24h,
        "buys_24h": buys_24h,
        "sells_24h": sells_24h,
        "transactions_24h": transactions_24h,
        "volume_liquidity_ratio": volume_liquidity_ratio,
        "price_usd": safe_float(
            pair.get("priceUsd")
        ),
        "dex_url": pair.get("url"),
        "pair_address": pair.get("pairAddress"),
        "dex_id": pair.get("dexId"),
    }


def evaluate_token_activity(metrics):
    """
    Decide whether a $5M+ token is active enough
    to use for smart-wallet discovery.
    """

    reasons = []

    effective_market_cap = safe_float(
        metrics.get("effective_market_cap")
    )

    liquidity = safe_float(
        metrics.get("liquidity")
    )

    volume_24h = safe_float(
        metrics.get("volume_24h")
    )

    transactions_24h = safe_int(
        metrics.get("transactions_24h")
    )

    volume_liquidity_ratio = safe_float(
        metrics.get("volume_liquidity_ratio")
    )

    if effective_market_cap < MARKET_CAP_LIMIT:
        reasons.append(
            "DexScreener valuation below "
            f"{format_usd(MARKET_CAP_LIMIT)}"
        )

    if liquidity < MIN_LIQUIDITY_USD:
        reasons.append(
            "liquidity below "
            f"{format_usd(MIN_LIQUIDITY_USD)}"
        )

    if volume_24h < MIN_VOLUME_24H_USD:
        reasons.append(
            "24h volume below "
            f"{format_usd(MIN_VOLUME_24H_USD)}"
        )

    if transactions_24h < MIN_TRANSACTIONS_24H:
        reasons.append(
            f"fewer than {MIN_TRANSACTIONS_24H} "
            "transactions in 24 hours"
        )

    if (
        liquidity > 0
        and volume_liquidity_ratio
        < MIN_VOLUME_LIQUIDITY_RATIO
    ):
        reasons.append(
            "24h volume is below "
            f"{MIN_VOLUME_LIQUIDITY_RATIO:.1%} "
            "of liquidity"
        )

    return not reasons, reasons


def filter_active_tokens(candidates):
    """
    Validate each Blockscout candidate using DexScreener.

    Only genuinely active tokens are passed to
    the top-holder collection stage.
    """

    active_tokens = []
    rejected_tokens = []

    print(
        "Checking token activity through DexScreener...\n"
    )

    for index, token in enumerate(
        candidates,
        start=1,
    ):
        token_name = (
            token.get("name") or "Unknown"
        )

        token_symbol = (
            token.get("symbol") or "UNKNOWN"
        )

        contract = str(
            token.get("address_hash") or ""
        ).lower()

        print(
            f"[{index}/{len(candidates)}] "
            f"Checking {token_name} ({token_symbol})..."
        )

        if not contract:
            reason = "Missing token contract"

            print(f"  REJECTED — {reason}")

            rejected_tokens.append(
                {
                    "name": token_name,
                    "symbol": token_symbol,
                    "contract": contract,
                    "reasons": [reason],
                }
            )

            continue

        pairs = fetch_dexscreener_pairs(
            contract
        )

        if not pairs:
            reason = (
                "No Robinhood Chain DexScreener pair found"
            )

            print(f"  REJECTED — {reason}")

            rejected_tokens.append(
                {
                    "name": token_name,
                    "symbol": token_symbol,
                    "contract": contract,
                    "reasons": [reason],
                }
            )

            time.sleep(DEXSCREENER_DELAY)
            continue

        best_pair = select_best_pair(
            pairs
        )

        if best_pair is None:
            reason = "No usable liquidity pool"

            print(f"  REJECTED — {reason}")

            rejected_tokens.append(
                {
                    "name": token_name,
                    "symbol": token_symbol,
                    "contract": contract,
                    "reasons": [reason],
                }
            )

            time.sleep(DEXSCREENER_DELAY)
            continue

        metrics = extract_pair_metrics(
            best_pair
        )

        accepted, reasons = (
            evaluate_token_activity(metrics)
        )

        enriched_token = {
            **token,
            "dexscreener": metrics,
        }

        if accepted:
            active_tokens.append(
                enriched_token
            )

            print(
                "  ACCEPTED | "
                f"{metrics['valuation_type']}: "
                f"{format_usd(metrics['effective_market_cap'])} | "
                f"Liquidity: "
                f"{format_usd(metrics['liquidity'])} | "
                f"24h volume: "
                f"{format_usd(metrics['volume_24h'])} | "
                f"24h transactions: "
                f"{metrics['transactions_24h']}"
            )

        else:
            rejected_tokens.append(
                {
                    "name": token_name,
                    "symbol": token_symbol,
                    "contract": contract,
                    "blockscout_market_cap": safe_float(
                        token.get(
                            "circulating_market_cap"
                        )
                    ),
                    "dexscreener": metrics,
                    "reasons": reasons,
                }
            )

            print(
                "  REJECTED — "
                + "; ".join(reasons)
            )

        time.sleep(DEXSCREENER_DELAY)

    active_tokens.sort(
        key=lambda token: safe_float(
            (
                token.get("dexscreener")
                or {}
            ).get("effective_market_cap")
        ),
        reverse=True,
    )

    with open(
        ACTIVE_TOKENS_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            active_tokens,
            file,
            indent=4,
        )

    with open(
        REJECTED_TOKENS_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            rejected_tokens,
            file,
            indent=4,
        )

    print("\nToken activity filtering finished.")
    print(
        f"Active tokens approved: "
        f"{len(active_tokens)}"
    )
    print(
        f"Inactive or misleading tokens rejected: "
        f"{len(rejected_tokens)}"
    )
    print(
        f"Approved token list saved to: "
        f"{ACTIVE_TOKENS_FILE}"
    )
    print(
        f"Rejected token list saved to: "
        f"{REJECTED_TOKENS_FILE}\n"
    )

    return active_tokens


# =========================================================
# HOLDER COLLECTION
# =========================================================

def fetch_top_wallets(token):
    """
    Fetch the first holder page and retain
    normal wallet addresses only.
    """

    contract = token.get("address_hash")

    token_name = (
        token.get("name") or "Unknown"
    )

    token_symbol = (
        token.get("symbol") or "UNKNOWN"
    )

    holders_url = (
        "https://robinhoodchain.blockscout.com/api/v2/"
        f"tokens/{contract}/holders"
    )

    data = get_json(
        holders_url,
        description=(
            f"{token_symbol} holder request"
        ),
    )

    if not isinstance(data, dict):
        return []

    wallets = []

    for holder in data.get("items") or []:
        address_data = (
            holder.get("address") or {}
        )

        wallet = address_data.get("hash")

        if not wallet:
            continue

        wallet_lower = wallet.lower()

        if wallet_lower in ZERO_ADDRESSES:
            continue

        # Exclude liquidity pools, token contracts
        # and other smart contracts.
        if address_data.get("is_contract") is True:
            continue

        # Exclude addresses flagged by Blockscout.
        if address_data.get("is_scam") is True:
            continue

        wallets.append(
            {
                "wallet": wallet,
                "balance": holder.get("value"),
                "token_name": token_name,
                "token_symbol": token_symbol,
                "token_contract": contract,
            }
        )

    return wallets


# =========================================================
# WALLET DATABASE
# =========================================================

def build_wallet_database(tokens):
    """
    Count which approved active tokens
    each normal wallet appears in.
    """

    database = defaultdict(
        lambda: {
            "count": 0,
            "tokens": [],
        }
    )

    for index, token in enumerate(
        tokens,
        start=1,
    ):
        token_name = (
            token.get("name") or "Unknown"
        )

        token_symbol = (
            token.get("symbol") or "UNKNOWN"
        )

        contract = token.get("address_hash")

        dex_metrics = (
            token.get("dexscreener") or {}
        )

        effective_market_cap = safe_float(
            dex_metrics.get(
                "effective_market_cap"
            )
        )

        volume_24h = safe_float(
            dex_metrics.get("volume_24h")
        )

        liquidity = safe_float(
            dex_metrics.get("liquidity")
        )

        print(
            f"[{index}/{len(tokens)}] Processing "
            f"{token_name} ({token_symbol}) | "
            f"Market cap: "
            f"{format_usd(effective_market_cap)} | "
            f"24h volume: "
            f"{format_usd(volume_24h)}"
        )

        holders = fetch_top_wallets(
            token
        )

        # Prevent the same wallet being counted twice
        # for the same token.
        seen_in_this_token = set()

        for holder in holders:
            wallet = holder["wallet"]
            wallet_key = wallet.lower()

            if wallet_key in seen_in_this_token:
                continue

            seen_in_this_token.add(
                wallet_key
            )

            database[wallet_key]["count"] += 1

            database[wallet_key]["tokens"].append(
                {
                    "name": token_name,
                    "symbol": token_symbol,
                    "contract": contract,
                    "balance": holder["balance"],
                    "market_cap": effective_market_cap,
                    "volume_24h": volume_24h,
                    "liquidity": liquidity,
                    "dex_url": dex_metrics.get(
                        "dex_url"
                    ),
                }
            )

        print(
            "  Normal top-holder wallets found: "
            f"{len(seen_in_this_token)}"
        )

        time.sleep(REQUEST_DELAY)

    return dict(database)


# =========================================================
# SAVE RESULTS
# =========================================================

def save_results(wallet_database):
    """
    Rebuild the wallet files from scratch
    using approved active tokens only.
    """

    ranked_wallets = dict(
        sorted(
            wallet_database.items(),
            key=lambda item: (
                item[1]["count"]
            ),
            reverse=True,
        )
    )

    smart_wallet_candidates = {
        wallet: information
        for wallet, information
        in ranked_wallets.items()
        if information["count"] >= 2
    }

    # "w" replaces the old contaminated files.
    with open(
        WALLETS_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            ranked_wallets,
            file,
            indent=4,
        )

    with open(
        SMART_CANDIDATES_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            smart_wallet_candidates,
            file,
            indent=4,
        )

    print("\nFinished.")
    print(
        f"Unique normal wallets saved: "
        f"{len(ranked_wallets)}"
    )
    print(
        "Wallets appearing in at least two "
        "approved active tokens: "
        f"{len(smart_wallet_candidates)}"
    )
    print(
        f"Saved full database to: "
        f"{WALLETS_FILE}"
    )
    print(
        "Saved repeated-wallet shortlist to: "
        f"{SMART_CANDIDATES_FILE}"
    )

    print("\nTop repeated wallets:")
    print("-" * 90)

    if not smart_wallet_candidates:
        print(
            "No wallets appeared in at least "
            "two approved active tokens."
        )
        return

    for wallet, information in list(
        smart_wallet_candidates.items()
    )[:20]:
        token_symbols = [
            token["symbol"]
            for token in information["tokens"]
        ]

        print(
            f"{wallet} | "
            f"{information['count']} tokens | "
            f"{', '.join(token_symbols)}"
        )


# =========================================================
# MAIN
# =========================================================

def main():
    blockscout_candidates = (
        fetch_blockscout_candidates()
    )

    if not blockscout_candidates:
        print(
            "No Blockscout tokens above "
            f"{format_usd(MARKET_CAP_LIMIT)} were found."
        )
        return

    active_tokens = filter_active_tokens(
        blockscout_candidates
    )

    if not active_tokens:
        print(
            "No tokens passed the activity filters. "
            "The wallet database was not rebuilt."
        )
        return

    wallet_database = build_wallet_database(
        active_tokens
    )

    save_results(
        wallet_database
    )


if __name__ == "__main__":
    main()