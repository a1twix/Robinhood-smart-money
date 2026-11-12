import json
from pathlib import Path


INPUT_FILE = Path("top_early_traders.json")
OUTPUT_FILE = Path("top_early_traders_summary.txt")


def format_number(value):
    if value is None:
        return "Unknown"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    if abs(number) >= 1_000_000:
        return f"{number:,.2f}"

    return f"{number:,.6f}".rstrip("0").rstrip(".")


def main():
    if not INPUT_FILE.exists():
        print(f"Could not find {INPUT_FILE}")
        return

    with INPUT_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    results = data.get("results", [])

    lines = []

    for result in results:
        token = result.get("token", {})
        traders = result.get("top_traders", [])

        name = token.get("name", "Unknown")
        symbol = token.get("symbol", "UNKNOWN")
        contract = token.get("contract", "Unknown")

        lines.append("=" * 100)
        lines.append(f"{name} ({symbol})")
        lines.append(f"Contract: {contract}")
        lines.append("=" * 100)

        if not traders:
            lines.append("No traders met the minimum profit requirement.")
            lines.append("")
            continue

        for position, trader in enumerate(traders, start=1):
            wallet = trader.get("wallet", "Unknown")
            entry = trader.get("delay_after_launch", "Unknown")

            exit_time = (
                trader.get("time_to_final_counted_exit")
                or trader.get("time_to_first_exit")
                or "Unknown"
            )

            profit = trader.get("realised_profit")
            roi = trader.get("realised_roi")
            asset = trader.get("ranking_asset", "")
            sold = (
                trader.get("cumulative_percentage_sold")
                or trader.get("first_exit_percentage_sold")
            )
            balance = trader.get("current_balance")
            complete = trader.get("analysis_complete", True)

            lines.append(f"#{position}")
            lines.append(f"Wallet: {wallet}")
            lines.append(f"Early buyer rank: #{trader.get('early_buy_rank', 'Unknown')}")
            lines.append(f"Entry after launch: {entry}")
            lines.append(f"Exit after entry: {exit_time}")
            lines.append(
                f"Realised profit: {format_number(profit)} {asset}"
            )

            if roi is not None:
                lines.append(f"Realised ROI: {float(roi):,.2f}%")
            else:
                lines.append("Realised ROI: Unknown")

            if sold is not None:
                lines.append(f"Original position sold: {float(sold):,.2f}%")

            lines.append(
                f"Current token balance: {format_number(balance)}"
            )

            if not complete:
                lines.append("Warning: incomplete API data")

            lines.append("-" * 100)

        lines.append("")

    report = "\n".join(lines)

    print(report)

    OUTPUT_FILE.write_text(
        report,
        encoding="utf-8",
    )

    print(f"\nReadable report saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()