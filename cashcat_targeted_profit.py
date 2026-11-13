import json
from pathlib import Path

import conviction_holder_analyzer as engine

CHECKPOINT = Path("conviction_holder_checkpoint.json")
OUTPUT = Path("cashcat_targeted_profit_results.json")
TARGET_RANKS = {18, 25}
SNAPSHOT_BLOCK = 113544308

def main():
    if OUTPUT.exists():
        raise SystemExit("Output already exists; refusing to overwrite.")

    checkpoint = json.loads(CHECKPOINT.read_text())
    token = checkpoint["token"]

    if token.get("symbol", "").upper() != "CASHCAT":
        raise SystemExit("Checkpoint is not CASHCAT.")

    contract = token["contract"]
    decimals = int(token["decimals"])

    targets = [
        holder for holder in checkpoint["holders_checked"]
        if holder.get("holder_rank") in TARGET_RANKS
    ]

    if {h["holder_rank"] for h in targets} != TARGET_RANKS:
        raise SystemExit("Both target holders were not found.")

    engine.load_persistent_api_caches()

    results = []

    try:
        for holder in targets:
            wallet = holder["wallet"]
            rank = holder["holder_rank"]

            print(f"\nAnalysing CASHCAT holder #{rank}: {wallet}")

            rows = engine.fetch_token_transfers(
                contract=contract,
                address=wallet,
                start_block=0,
                max_pages=engine.MAX_HOLDER_WALLET_PAGES,
                end_block=SNAPSHOT_BLOCK,
            )

            if rows is None:
                position = {
                    "position_status": "INCOMPLETE",
                    "reason": "Transfer history unavailable or truncated",
                }
            else:
                position = engine.reconstruct_holder_position(
                    wallet=wallet,
                    wallet_rows=rows,
                    contract=contract,
                    decimals=decimals,
                    current_balance=holder["current_balance"],
                    snapshot_block=SNAPSHOT_BLOCK,
                    history_start_block=None,
                )

            results.append({
                "holder_rank": rank,
                "wallet": wallet,
                "original_whale_holdings": holder.get("whale_holdings"),
                "full_position_analysis": position,
            })

            print(
                "Position:",
                position.get("position_status"),
                "|",
                position.get("reason"),
            )

            # Save after each wallet, without touching original reports.
            OUTPUT.write_text(json.dumps({
                "token": token,
                "snapshot_block": SNAPSHOT_BLOCK,
                "analysis_complete": False,
                "results": results,
            }, indent=2))

    finally:
        engine.save_persistent_api_caches()

    print(f"\nResults saved to {OUTPUT}")

if __name__ == "__main__":
    main()
