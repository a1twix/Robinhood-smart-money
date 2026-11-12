import json
from pathlib import Path

CANDIDATES_FILE = Path("smart_wallet_candidates.json")


def load_candidates():
    if not CANDIDATES_FILE.exists():
        raise FileNotFoundError(
            "smart_wallet_candidates.json was not found. "
            "Run wallet_database.py first."
        )

    with CANDIDATES_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def main():
    candidates = load_candidates()

    print(f"Smart-wallet candidates loaded: {len(candidates)}")
    print("-" * 80)

    for wallet, information in candidates.items():
        print(f"\nWallet: {wallet}")
        print(f"Successful tokens held: {information.get('count', 0)}")

        for token in information.get("tokens", []):
            print(
                f"  - {token.get('name')} "
                f"({token.get('symbol')}) | "
                f"Contract: {token.get('contract')} | "
                f"Current balance: {token.get('balance')}"
            )


if __name__ == "__main__":
    main()