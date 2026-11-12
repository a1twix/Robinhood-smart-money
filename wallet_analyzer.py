#!/usr/bin/env python3
"""Wallet due-diligence analyser for Robinhood Chain."""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import early_buyer_analyzer as engine

OUTPUT_FILE = Path("wallet_analysis_results.json")
SUMMARY_FILE = Path("wallet_analysis_summary.txt")
CHECKPOINT_DIR = Path("wallet_analysis_checkpoints")
MAX_WALLET_HISTORY_PAGES = 20
PAGE_SIZE = 1000
QUOTE_SYMBOLS = set(engine.ETH_SYMBOLS) | set(engine.STABLE_SYMBOLS)


def norm(value: Any) -> str:
    return engine.normalise_address(value)


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
    tmp.replace(path)


def checkpoint_path(wallet: str) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return CHECKPOINT_DIR / f"{wallet}.json"


def load_checkpoint(wallet: str) -> dict[str, Any]:
    path = checkpoint_path(wallet)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_checkpoint(wallet: str, completed: dict[str, Any]) -> None:
    write_json(checkpoint_path(wallet), {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "wallet": wallet,
        "completed_tokens": completed,
    })
    engine.save_persistent_api_caches()


def fetch_wallet_transfers(wallet: str) -> list[dict[str, Any]] | None:
    rows_all = []
    seen = set()
    print(f"\nFetching token-transfer history for {wallet}...")

    for page in range(1, MAX_WALLET_HISTORY_PAGES + 1):
        data = engine.request_json(
            engine.PRO_LEGACY_API_URL,
            params={
                "module": "account", "action": "tokentx", "address": wallet,
                "startblock": 0, "endblock": 99999999, "page": page,
                "offset": PAGE_SIZE, "sort": "asc",
            },
            description=f"Wallet token-transfer page {page}",
        )
        if data is None:
            return None
        rows = data.get("result") or [] if isinstance(data, dict) else []
        if not isinstance(rows, list):
            engine.register_api_failure(
                f"Wallet token-transfer page {page}", "unexpected response format"
            )
            return None

        print(f"  Page {page}: {len(rows)} transfer(s)")
        if not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (
                str(row.get("hash") or row.get("transaction_hash") or "").lower(),
                str(row.get("logIndex") or row.get("log_index") or ""),
                str(row.get("contractAddress") or "").lower(),
                str(row.get("from") or "").lower(),
                str(row.get("to") or "").lower(),
            )
            if key not in seen:
                seen.add(key)
                rows_all.append(row)

        if len(rows) < PAGE_SIZE:
            break
        time.sleep(engine.REQUEST_DELAY)

    rows_all.sort(key=lambda r: (
        engine.safe_int(r.get("blockNumber")),
        engine.safe_int(r.get("transactionIndex")),
        engine.safe_int(r.get("logIndex") or r.get("log_index")),
    ))
    return rows_all


def row_contract(row: dict[str, Any]) -> str:
    return norm(
        row.get("contractAddress")
        or row.get("contract_address")
        or engine.token_contract_from_transfer(row)
    )


def discover_tokens(wallet: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = {}
    for row in rows:
        if norm(row.get("to")) != wallet:
            continue
        contract = row_contract(row)
        if not contract or contract in engine.ZERO_ADDRESSES:
            continue

        symbol = str(row.get("tokenSymbol") or "UNKNOWN").upper()
        decimals = engine.safe_int(row.get("tokenDecimal"), 18)
        if symbol == "UNKNOWN":
            meta = engine.rpc_token_metadata(contract)
            symbol = str(meta.get("symbol") or "UNKNOWN").upper()
            decimals = engine.safe_int(meta.get("decimals"), decimals)

        if symbol in QUOTE_SYMBOLS:
            continue

        block = engine.safe_int(row.get("blockNumber"))
        item = {
            "name": str(row.get("tokenName") or symbol or "Unknown"),
            "symbol": symbol,
            "contract": contract,
            "decimals": decimals,
            "first_seen_block": block,
        }
        if contract not in found or block < found[contract]["first_seen_block"]:
            found[contract] = item

    return sorted(found.values(), key=lambda x: x["first_seen_block"])


def first_genuine_buy(wallet: str, token: dict[str, Any],
                      wallet_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    contract = token["contract"]
    decimals = engine.safe_int(token.get("decimals"), 18)
    hashes, seen = [], set()

    for row in wallet_rows:
        if row_contract(row) != contract or norm(row.get("to")) != wallet:
            continue
        txh = str(row.get("hash") or row.get("transaction_hash") or "").lower()
        if txh and txh not in seen:
            seen.add(txh)
            hashes.append(txh)

            # Wallet due-diligence safety cap:
            # only inspect the earliest incoming transactions for this token.
            # Prevents very active tokens (e.g. stock tokens) monopolising the run.
            if len(hashes) >= 40:
                break

    if len(hashes) >= 40:
        print("      Incoming transaction search capped at 40.")

    for txh in hashes:
        tx = engine.fetch_transaction(txh)
        if (
            not tx or not engine.transaction_succeeded(tx)
            or not engine.is_swap(tx) or norm(tx.get("from")) != wallet
        ):
            continue

        received = 0.0
        for transfer in tx.get("token_transfers") or []:
            if (
                engine.token_contract_from_transfer(transfer) == contract
                and norm(transfer.get("to")) == wallet
            ):
                received += engine.transfer_amount(transfer, decimals)
        if received <= 0:
            continue

        flows = engine.quote_flows(
            tx, txh, wallet, contract, include_internal_eth=False
        )
        spent_eth = engine.sum_symbols(flows["outgoing"], engine.ETH_SYMBOLS)
        spent_stable = engine.sum_symbols(flows["outgoing"], engine.STABLE_SYMBOLS)
        if spent_eth <= 0 and spent_stable <= 0:
            continue

        return {
            "wallet": wallet,
            "early_buy_rank": None,
            "entry_transaction_hash": txh,
            "entry_block": engine.transaction_block(tx),
            "entry_time_utc": engine.transaction_time(tx),
            "initial_tokens_received": received,
            "initial_spent_eth": spent_eth,
            "initial_spent_stable": spent_stable,
            "entry_method": engine.transaction_method(tx),
        }
    return None


def launch_reference(contract: str, decimals: int):
    """Best-effort launch context; failure here must not invalidate P&L."""
    failures_before = engine.API_FAILURE_COUNT
    rows = engine.fetch_token_transfers(
        contract, max_pages=engine.MAX_TOKEN_PAGES
    )
    if rows is None or not rows or engine.API_FAILURE_COUNT > failures_before:
        return None, 0

    first_block = engine.first_token_block(rows) or 0
    buyers = engine.find_early_buyers(rows, contract, decimals)
    if buyers:
        return (
            engine.parse_time(buyers[0].get("entry_time_utc")),
            engine.safe_int(buyers[0].get("entry_block")),
        )
    return None, first_block


def fmt(value: Any, places: int = 6) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return "N/A"


def summary_text(wallet: str, results: list[dict[str, Any]]) -> str:
    exact = [
        r for r in results
        if r.get("analysis_complete") is True
        and r.get("ranking_asset") == "ETH"
        and r.get("realised_profit") is not None
    ]
    wins = [r for r in exact if engine.safe_float(r.get("realised_profit")) > 0]
    losses = [r for r in exact if engine.safe_float(r.get("realised_profit")) < 0]
    invested = sum(engine.safe_float(r.get("cumulative_cost_basis_eth")) for r in exact)
    returned = sum(engine.safe_float(r.get("cumulative_received_eth")) for r in exact)
    profit = sum(engine.safe_float(r.get("realised_profit")) for r in exact)
    decided = len(wins) + len(losses)

    lines = [
        "=" * 100,
        "ROBINHOOD CHAIN WALLET DUE-DILIGENCE REPORT",
        "=" * 100,
        f"Wallet: {wallet}",
        f"Genuine token buys analysed: {len(results)}",
        f"Completed exact ETH positions: {len(exact)}",
        f"Profitable: {len(wins)}",
        f"Losing: {len(losses)}",
        f"Win rate: {(len(wins)/decided*100):.2f}%" if decided else "Win rate: N/A",
        f"Counted ETH cost basis: {fmt(invested)} ETH",
        f"Counted ETH proceeds: {fmt(returned)} ETH",
        f"Realised ETH profit: {fmt(profit)} ETH",
        "",
        "NOTE: Each token is measured from the wallet's first genuine buy allocation.",
        "Later additional buys are not merged into that position. Mixed/stable quote",
        "positions are shown individually but excluded from aggregate ETH P&L.",
        "",
        "POSITIONS",
        "-" * 100,
    ]

    ranked = sorted(
        results,
        key=lambda r: (
            r.get("realised_profit") is not None,
            engine.safe_float(r.get("realised_profit"), -1e18),
        ),
        reverse=True,
    )

    for i, r in enumerate(ranked, 1):
        t = r.get("token") or {}
        lines += [
            f"{i}. {t.get('name')} ({t.get('symbol')})",
            f"   Contract: {t.get('contract')}",
            f"   Entry: {r.get('entry_time_utc')}",
            f"   Entry after launch: {r.get('delay_after_launch')}",
            f"   Initial ETH spent: {fmt(r.get('initial_spent_eth'))}",
            f"   Initial stable spent: {fmt(r.get('initial_spent_stable'))}",
            f"   Confirmed sells: {r.get('confirmed_sell_count')}",
            f"   Original position sold: {fmt(r.get('cumulative_percentage_sold'), 2)}%",
            f"   ETH proceeds: {fmt(r.get('cumulative_received_eth'))}",
            f"   Realised profit: {fmt(r.get('realised_profit'))} {r.get('ranking_asset') or ''}".rstrip(),
            f"   Realised ROI: {fmt(r.get('realised_roi'), 2)}%",
            f"   Current token balance: {fmt(r.get('current_balance'))}",
            f"   Profit status: {r.get('profit_status')}",
            f"   Analysis complete: {r.get('analysis_complete')}",
            "-" * 100,
        ]
    return "\n".join(lines)


def main() -> None:
    if not engine.BLOCKSCOUT_API_KEY:
        raise RuntimeError("BLOCKSCOUT_API_KEY is not available in the environment.")

    engine.load_persistent_api_caches()
    wallet = norm(sys.argv[1] if len(sys.argv) > 1 else input(
        "Enter wallet address to investigate: "
    ).strip())

    if not wallet.startswith("0x") or len(wallet) != 42:
        raise ValueError("A valid 0x-prefixed 42-character wallet address is required.")

    print("\n" + "=" * 100)
    print("WALLET DUE-DILIGENCE ANALYSER")
    print("=" * 100)
    print(f"Wallet: {wallet}")

    info = engine.fetch_address_info(wallet)
    if info.get("is_contract") is True:
        raise ValueError("The supplied address is a contract, not an EOA wallet.")

    wallet_rows = fetch_wallet_transfers(wallet)
    if wallet_rows is None:
        print("Wallet history retrieval was incomplete; stopping safely.")
        engine.save_persistent_api_caches()
        return

    candidates = discover_tokens(wallet, wallet_rows)
    print(f"\nCandidate non-quote tokens received: {len(candidates)}")

    cp = load_checkpoint(wallet)
    saved = cp.get("completed_tokens") or {}
    completed = {
        str(k).lower(): v for k, v in saved.items() if isinstance(v, dict)
    } if isinstance(saved, dict) else {}
    results = []

    for i, token in enumerate(candidates, 1):
        contract = token["contract"]
        print("\n" + "=" * 100)
        print(f"[{i}/{len(candidates)}] {token['name']} ({token['symbol']})")
        print("=" * 100)

        old = completed.get(contract)
        if isinstance(old, dict) and old.get("analysis_complete") is True:
            print("Reusing completed checkpoint.")
            results.append(old)
            continue

        buyer = first_genuine_buy(wallet, token, wallet_rows)
        if buyer is None:
            print("No genuine wallet-initiated DEX buy found. Skipping.")
            continue

        print(
            f"Genuine buy found: {buyer['entry_time_utc']} | "
            f"ETH spent {fmt(buyer['initial_spent_eth'])} | "
            f"stable spent {fmt(buyer['initial_spent_stable'])}"
        )

        # Lightweight wallet profiling:
        # Do NOT rescan the token's launch history / other early buyers.
        # We only care about this wallet's genuine buy and subsequent exits.
        result = engine.analyse_first_entry_first_exit(
            buyer=buyer,
            contract=contract,
            decimals=engine.safe_int(token.get("decimals"), 18),
            launch_time=None,
            launch_block=0,
        )
        result["token"] = {
            "name": token["name"], "symbol": token["symbol"],
            "contract": contract, "decimals": token["decimals"],
        }
        result["wallet_profile_note"] = (
            "Measures first genuine buy allocation; later buys are not merged."
        )

        # analyse_first_entry_first_exit owns its own failure accounting.
        if result.get("analysis_complete") is True:
            completed[contract] = result
            results.append(result)
            save_checkpoint(wallet, completed)
            print("Position completed and checkpointed.")
        else:
            print("Position incomplete; it remains pending.")

    known = {
        str((r.get("token") or {}).get("contract") or "").lower()
        for r in results
    }
    for contract, result in completed.items():
        if contract not in known:
            results.append(result)

    text = summary_text(wallet, results)
    write_json(OUTPUT_FILE, {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "wallet": wallet,
        "wallet_transfer_rows_fetched": len(wallet_rows),
        "candidate_tokens_received": len(candidates),
        "positions_completed": len(results),
        "api_failure_count": engine.API_FAILURE_COUNT,
        "api_failure_log": engine.API_FAILURE_LOG,
        "results": results,
    })
    SUMMARY_FILE.write_text(text + "\n", encoding="utf-8")
    engine.save_persistent_api_caches()

    print("\n" + text)
    print(f"\nJSON report saved to: {OUTPUT_FILE}")
    print(f"Readable report saved to: {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
