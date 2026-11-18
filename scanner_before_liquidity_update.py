import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


BLOCKSCOUT_URL = "https://api.blockscout.com/4663/api/v2/tokens"
DEXSCREENER_URL = "https://api.dexscreener.com/token-pairs/v1"
DEXSCREENER_CHAIN_ID = "robinhood"

MIN_MARKET_CAP_USD = 6_000_000
MAX_MARKET_CAP_USD = 100_000_000
MIN_LIQUIDITY_USD = 75_000
MIN_VOLUME_24H_USD = 25_000
MIN_TRANSACTIONS_24H = 40
MIN_BUYS_24H = 10
MIN_SELLS_24H = 5
MIN_LIQUIDITY_TO_MARKET_CAP = 0.01
MIN_VOLUME_TO_LIQUIDITY = 0.05
MIN_SELL_TO_BUY_RATIO = 0.03

MAX_PAGES = 100
MAX_RETRIES = 5
REQUEST_TIMEOUT = 30
BLOCKSCOUT_DELAY = 3
DEXSCREENER_DELAY = 0.35
RETRY_DELAYS = (5, 10, 20, 40, 60)

OUTPUT_FILE = Path("scanner_qualifying_tokens.json")
HISTORY_FILE = Path("scanner_seen_qualifying_tokens.json")
REJECTED_FILE = Path("scanner_rejected_tokens.json")

EXCLUDED_SYMBOLS = {
    "ETH", "WETH", "BTC", "WBTC", "USDC", "USDT",
    "DAI", "USDE", "USDG", "SYRUPUSDG",
}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def format_usd(value: Any) -> str:
    number = safe_float(value)

    if number >= 1_000_000_000:
        return f"${number / 1_000_000_000:.2f}B"
    if number >= 1_000_000:
        return f"${number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"${number / 1_000:.2f}K"

    return f"${number:,.2f}"


def request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    description: str = "request",
) -> Any:
    for attempt in range(1, MAX_RETRIES + 1):
        wait_time = RETRY_DELAYS[
            min(attempt - 1, len(RETRY_DELAYS) - 1)
        ]

        try:
            request_params = dict(params or {})

            if url == BLOCKSCOUT_URL:
                request_params["apikey"] = os.getenv("BLOCKSCOUT_API_KEY")

            response = requests.get(
                url,
                params=request_params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 429:
                retry_after = safe_int(
                    response.headers.get("Retry-After"),
                    wait_time,
                )
                print(
                    f"{description} rate limited. "
                    f"Waiting {retry_after} seconds..."
                )
                time.sleep(retry_after)
                continue

            if response.status_code >= 500:
                print(
                    f"{description} server error "
                    f"{response.status_code}. "
                    f"Retrying in {wait_time} seconds..."
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
                return None

            print(
                f"{description} error: {error}. "
                f"Retrying in {wait_time} seconds..."
            )
            time.sleep(wait_time)

    return None


def fetch_blockscout_tokens() -> list[dict[str, Any]]:
    tokens_by_contract: dict[str, dict[str, Any]] = {}
    params: dict[str, Any] = {}
    seen_cursors: set[tuple[tuple[str, str], ...]] = set()

    for page in range(1, MAX_PAGES + 1):
        data = request_json(
            BLOCKSCOUT_URL,
            params=params,
            description=f"Blockscout page {page}",
        )

        if not isinstance(data, dict):
            break

        items = data.get("items") or []

        for token in items:
            if not isinstance(token, dict):
                continue

            contract = str(
                token.get("address_hash") or ""
            ).lower()

            if contract:
                tokens_by_contract[contract] = token

        print(
            f"Page {page}: fetched {len(items)} tokens "
            f"({len(tokens_by_contract)} unique total)"
        )

        next_page = data.get("next_page_params")

        if not next_page:
            print("Reached the final Blockscout page.")
            break

        params = {
            key: (
                str(value).lower()
                if isinstance(value, bool)
                else value
            )
            for key, value in next_page.items()
        }

        cursor = tuple(
            sorted(
                (key, str(value))
                for key, value in params.items()
            )
        )

        if cursor in seen_cursors:
            print(
                "Blockscout repeated the page cursor. "
                "Stopping safely."
            )
            break

        seen_cursors.add(cursor)
        time.sleep(BLOCKSCOUT_DELAY)

    return list(tokens_by_contract.values())


def fetch_pairs(contract: str) -> list[dict[str, Any]]:
    url = (
        f"{DEXSCREENER_URL}/"
        f"{DEXSCREENER_CHAIN_ID}/{contract}"
    )

    data = request_json(
        url,
        description=f"DexScreener {contract[:10]}...",
    )

    if not isinstance(data, list):
        return []

    return [
        pair
        for pair in data
        if isinstance(pair, dict)
        and str(pair.get("chainId") or "").lower()
        == DEXSCREENER_CHAIN_ID
    ]


def select_best_pair(
    pairs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not pairs:
        return None

    return max(
        pairs,
        key=lambda pair: safe_float(
            (pair.get("liquidity") or {}).get("usd")
        ),
    )


def pair_metrics(pair: dict[str, Any]) -> dict[str, Any]:
    liquidity = safe_float(
        (pair.get("liquidity") or {}).get("usd")
    )
    volume = safe_float(
        (pair.get("volume") or {}).get("h24")
    )

    txns = (pair.get("txns") or {}).get("h24") or {}
    buys = safe_int(txns.get("buys"))
    sells = safe_int(txns.get("sells"))

    market_cap = safe_float(pair.get("marketCap"))
    fdv = safe_float(pair.get("fdv"))
    valuation = market_cap if market_cap > 0 else fdv

    pair_created_ms = safe_int(pair.get("pairCreatedAt"))
    pair_created_utc = None
    pair_age_hours = None

    if pair_created_ms > 0:
        created = datetime.fromtimestamp(
            pair_created_ms / 1000,
            timezone.utc,
        )
        pair_created_utc = created.isoformat()
        pair_age_hours = (
            datetime.now(timezone.utc) - created
        ).total_seconds() / 3600

    return {
        "market_cap": market_cap,
        "fdv": fdv,
        "effective_market_cap": valuation,
        "valuation_type": (
            "Market cap" if market_cap > 0 else "FDV"
        ),
        "liquidity": liquidity,
        "volume_24h": volume,
        "buys_24h": buys,
        "sells_24h": sells,
        "transactions_24h": buys + sells,
        "liquidity_market_cap_ratio": (
            liquidity / valuation if valuation > 0 else 0
        ),
        "volume_liquidity_ratio": (
            volume / liquidity if liquidity > 0 else 0
        ),
        "sell_buy_ratio": (
            sells / buys if buys > 0 else 0
        ),
        "price_usd": safe_float(pair.get("priceUsd")),
        "price_change_24h": safe_float(
            (pair.get("priceChange") or {}).get("h24")
        ),
        "pair_created_at_utc": pair_created_utc,
        "pair_age_hours": pair_age_hours,
        "pair_address": pair.get("pairAddress"),
        "dex_id": pair.get("dexId"),
        "dex_url": pair.get("url"),
    }


def evaluate(metrics: dict[str, Any]) -> list[str]:
    reasons: list[str] = []

    market_cap = safe_float(
        metrics.get("effective_market_cap")
    )
    liquidity = safe_float(metrics.get("liquidity"))
    volume = safe_float(metrics.get("volume_24h"))
    buys = safe_int(metrics.get("buys_24h"))
    sells = safe_int(metrics.get("sells_24h"))
    transactions = safe_int(
        metrics.get("transactions_24h")
    )
    liq_mc = safe_float(
        metrics.get("liquidity_market_cap_ratio")
    )
    vol_liq = safe_float(
        metrics.get("volume_liquidity_ratio")
    )
    sell_buy = safe_float(
        metrics.get("sell_buy_ratio")
    )

    if market_cap < MIN_MARKET_CAP_USD:
        reasons.append(
            f"valuation below "
            f"{format_usd(MIN_MARKET_CAP_USD)}"
        )

    if market_cap > MAX_MARKET_CAP_USD:
        reasons.append(
            f"valuation above "
            f"{format_usd(MAX_MARKET_CAP_USD)}"
        )

    if liquidity < MIN_LIQUIDITY_USD:
        reasons.append(
            f"liquidity below "
            f"{format_usd(MIN_LIQUIDITY_USD)}"
        )

    if volume < MIN_VOLUME_24H_USD:
        reasons.append(
            f"24h volume below "
            f"{format_usd(MIN_VOLUME_24H_USD)}"
        )

    if transactions < MIN_TRANSACTIONS_24H:
        reasons.append(
            f"fewer than "
            f"{MIN_TRANSACTIONS_24H} 24h transactions"
        )

    if buys < MIN_BUYS_24H:
        reasons.append(
            f"fewer than {MIN_BUYS_24H} 24h buys"
        )

    if sells < MIN_SELLS_24H:
        reasons.append(
            f"fewer than {MIN_SELLS_24H} 24h sells"
        )

    if liq_mc < MIN_LIQUIDITY_TO_MARKET_CAP:
        reasons.append(
            "liquidity below "
            f"{MIN_LIQUIDITY_TO_MARKET_CAP:.1%} "
            "of valuation"
        )

    if vol_liq < MIN_VOLUME_TO_LIQUIDITY:
        reasons.append(
            "24h volume below "
            f"{MIN_VOLUME_TO_LIQUIDITY:.1%} "
            "of liquidity"
        )

    if sell_buy < MIN_SELL_TO_BUY_RATIO:
        reasons.append(
            "too few sells relative to buys"
        )

    return reasons


def save_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(value, file, indent=4)


def load_saved_tokens(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError) as error:
        print(f"Could not read {path}: {error}")
        return []

    if isinstance(data, dict):
        tokens = data.get("tokens") or []
    elif isinstance(data, list):
        tokens = data
    else:
        return []

    return [
        token
        for token in tokens
        if isinstance(token, dict)
    ]


def merge_unique_tokens(
    existing: list[dict[str, Any]],
    new_tokens: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for token in existing + new_tokens:
        contract = str(token.get("contract") or "").lower()
        if contract:
            merged[contract] = token

    return list(merged.values())


def main() -> None:
    # HISTORY_FILE remembers every token previously selected. On the first
    # updated run, seed it from the existing scanner output so old results
    # are not returned again. OUTPUT_FILE will then contain fresh tokens only.
    saved_tokens = load_saved_tokens(HISTORY_FILE)
    if not saved_tokens:
        saved_tokens = load_saved_tokens(OUTPUT_FILE)

    known_contracts = {
        str(token.get("contract") or "").lower()
        for token in saved_tokens
        if token.get("contract")
    }

    tokens = fetch_blockscout_tokens()

    candidates = []
    rejected = []
    skipped_known = 0

    for token in tokens:
        symbol = str(
            token.get("symbol") or ""
        ).upper()

        if symbol in EXCLUDED_SYMBOLS:
            continue

        blockscout_market_cap = safe_float(
            token.get("circulating_market_cap")
        )

        if (
            MIN_MARKET_CAP_USD
            <= blockscout_market_cap
            <= MAX_MARKET_CAP_USD
        ):
            contract = str(
                token.get("address_hash") or ""
            ).lower()

            if contract in known_contracts:
                skipped_known += 1
                continue

            candidates.append(token)

    candidates.sort(
        key=lambda token: safe_float(
            token.get("circulating_market_cap")
        ),
        reverse=True,
    )

    print("\n" + "=" * 100)
    print(f"Tokens scanned: {len(tokens)}")
    print(
        f"Blockscout candidates above "
        f"{format_usd(MIN_MARKET_CAP_USD)}: "
        f"{len(candidates)}"
    )
    print(f"Previously selected tokens skipped: {skipped_known}")
    print("=" * 100)

    qualifying = []

    for index, token in enumerate(
        candidates,
        start=1,
    ):
        name = token.get("name") or "Unknown"
        symbol = token.get("symbol") or "UNKNOWN"
        contract = str(
            token.get("address_hash") or ""
        ).lower()

        print(
            f"\n[{index}/{len(candidates)}] "
            f"Checking {name} ({symbol})..."
        )

        pairs = fetch_pairs(contract)

        if not pairs:
            rejected.append(
                {
                    "name": name,
                    "symbol": symbol,
                    "contract": contract,
                    "reasons": [
                        "No Robinhood DexScreener pair"
                    ],
                }
            )
            print(
                "REJECTED | No Robinhood "
                "DexScreener pair"
            )
            continue

        pair = select_best_pair(pairs)

        if pair is None:
            continue

        metrics = pair_metrics(pair)
        reasons = evaluate(metrics)

        result = {
            "name": name,
            "symbol": symbol,
            "contract": contract,
            "holders_count": token.get(
                "holders_count"
            ),
            "blockscout_market_cap": safe_float(
                token.get("circulating_market_cap")
            ),
            "dexscreener": metrics,
        }

        if reasons:
            rejected.append(
                {
                    **result,
                    "reasons": reasons,
                }
            )
            print(
                "REJECTED | "
                + "; ".join(reasons)
            )
        else:
            qualifying.append(result)
            print(
                "ACCEPTED | "
                f"{metrics['valuation_type']}: "
                f"{format_usd(metrics['effective_market_cap'])} | "
                f"Liquidity: "
                f"{format_usd(metrics['liquidity'])} | "
                f"Volume: "
                f"{format_usd(metrics['volume_24h'])} | "
                f"Txns: {metrics['transactions_24h']}"
            )

        time.sleep(DEXSCREENER_DELAY)

    qualifying.sort(
        key=lambda token: safe_float(
            token["dexscreener"][
                "effective_market_cap"
            ]
        ),
        reverse=True,
    )

    generated_at = datetime.now(
        timezone.utc
    ).isoformat()

    # Keep the normal scanner output fresh so early_buyer_analyzer.py
    # receives only newly selected tokens from this run.
    save_json(
        OUTPUT_FILE,
        {
            "generated_at_utc": generated_at,
            "count": len(qualifying),
            "tokens": qualifying,
        },
    )

    all_seen_tokens = merge_unique_tokens(
        saved_tokens,
        qualifying,
    )
    save_json(
        HISTORY_FILE,
        {
            "updated_at_utc": generated_at,
            "count": len(all_seen_tokens),
            "tokens": all_seen_tokens,
        },
    )

    save_json(
        REJECTED_FILE,
        {
            "generated_at_utc": generated_at,
            "count": len(rejected),
            "tokens": rejected,
        },
    )

    print("\n" + "=" * 110)
    print(
        "ACTIVE $6M-$100M TOKENS FOR "
        "EARLY-TRADER ANALYSIS"
    )
    print("=" * 110)

    for token in qualifying:
        metrics = token["dexscreener"]

        print(
            f"{token['name']} "
            f"({token['symbol']})\n"
            f"  Market cap/FDV: "
            f"{format_usd(metrics['effective_market_cap'])}\n"
            f"  Liquidity: "
            f"{format_usd(metrics['liquidity'])} "
            f"({metrics['liquidity_market_cap_ratio']:.2%})\n"
            f"  24h volume: "
            f"{format_usd(metrics['volume_24h'])}\n"
            f"  24h transactions: "
            f"{metrics['transactions_24h']} "
            f"({metrics['buys_24h']} buys / "
            f"{metrics['sells_24h']} sells)\n"
            f"  Pair age: "
            f"{metrics['pair_age_hours'] or 'Unknown'} hours\n"
            f"  Contract: {token['contract']}\n"
            f"  DexScreener: "
            f"{metrics.get('dex_url') or 'Unavailable'}"
        )
        print("-" * 110)

    print(
        f"\nSaved {len(qualifying)} fresh qualifying tokens "
        f"to {OUTPUT_FILE}"
    )
    print(
        f"Remembering {len(all_seen_tokens)} previously selected "
        f"tokens in {HISTORY_FILE}"
    )


if __name__ == "__main__":
    main()
