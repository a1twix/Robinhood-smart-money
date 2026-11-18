import json
import time
from pathlib import Path

import requests


BASE_URL = "https://robinhoodchain.blockscout.com/api/v2"
CANDIDATES_FILE = Path("smart_wallet_candidates.json")

MAX_PAGES = 30
MAX_RETRIES = 5
REQUEST_DELAY = 1.5


def get_json(url, params=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=30)

            if response.status_code == 429:
                print("Rate limited. Waiting 10 seconds...")
                time.sleep(10)
                continue

            response.raise_for_status()
            return response.json()

        except requests.RequestException as error:
            if attempt == MAX_RETRIES:
                print(f"Request failed: {error}")
                return None

            print(f"Request error: {error}")
            time.sleep(5)

    return None


def clean_params(params):
    return {
        key: str(value).lower() if isinstance(value, bool) else value
        for key, value in params.items()
    }


def load_candidates():
    if not CANDIDATES_FILE.exists():
        raise FileNotFoundError(
            "smart_wallet_candidates.json was not found."
        )

    with CANDIDATES_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def choose_wallet(candidates):
    print("Candidate wallets:\n")

    wallets = list(candidates.items())

    for number, (wallet, information) in enumerate(wallets, start=1):
        symbols = [
            token.get("symbol", "UNKNOWN")
            for token in information.get("tokens", [])
        ]

        print(
            f"{number}. {wallet} | "
            f"{information.get('count', 0)} tokens | "
            f"{', '.join(symbols)}"
        )

    selection = input(
        "\nEnter the candidate number to analyse: "
    ).strip()

    try:
        selected_index = int(selection) - 1
        return wallets[selected_index]
    except (ValueError, IndexError):
        raise ValueError("Invalid candidate number.")


def fetch_wallet_transfers(wallet):
    url = f"{BASE_URL}/addresses/{wallet}/token-transfers"

    all_transfers = []
    params = {}
    seen_cursors = set()

    print(f"\nFetching token-transfer history for {wallet}...\n")

    for page_number in range(1, MAX_PAGES + 1):
        data = get_json(url, params=params)

        if data is None:
            break

        items = data.get("items", [])
        all_transfers.extend(items)

        print(
            f"Page {page_number}: "
            f"{len(items)} transfers returned "
            f"({len(all_transfers)} total)"
        )

        next_page = data.get("next_page_params")

        if not next_page:
            break

        next_params = clean_params(next_page)

        cursor = tuple(
            sorted(
                (key, str(value))
                for key, value in next_params.items()
            )
        )

        if cursor in seen_cursors:
            print("Repeated cursor detected. Stopping safely.")
            break

        seen_cursors.add(cursor)
        params = next_params
        time.sleep(REQUEST_DELAY)

    return all_transfers


def normalise_address(value):
    if isinstance(value, str):
        return value.lower()

    if isinstance(value, dict):
        address_hash = value.get("hash")
        if address_hash:
            return address_hash.lower()

    return ""


def analyse_token(wallet, token, transfers):
    token_contract = str(token.get("contract") or "").lower()
    token_symbol = token.get("symbol", "UNKNOWN")
    token_name = token.get("name", "Unknown")

    matching = []

    for transfer in transfers:
        transfer_token = transfer.get("token") or {}

        transfer_contract = str(
            transfer_token.get("address_hash")
            or transfer_token.get("address")
            or ""
        ).lower()

        if transfer_contract == token_contract:
            matching.append(transfer)

    if not matching:
        print(f"\n{token_name} ({token_symbol})")
        print("  No matching transfer history found.")
        return

    matching.sort(
        key=lambda item: (
            item.get("timestamp") or "",
            item.get("block_number") or 0,
            item.get("log_index") or 0,
        )
    )

    first_transfer = matching[0]

    incoming = []
    outgoing = []

    for transfer in matching:
        from_address = normalise_address(transfer.get("from"))
        to_address = normalise_address(transfer.get("to"))

        if to_address == wallet.lower():
            incoming.append(transfer)

        if from_address == wallet.lower():
            outgoing.append(transfer)

    first_from = normalise_address(first_transfer.get("from"))
    first_to = normalise_address(first_transfer.get("to"))

    if first_to == wallet.lower():
        direction = "Incoming"
    elif first_from == wallet.lower():
        direction = "Outgoing"
    else:
        direction = "Unknown"

    print(f"\n{token_name} ({token_symbol})")
    print(f"  Contract: {token_contract}")
    print(
        f"  First matching transfer: "
        f"{first_transfer.get('timestamp')}"
    )
    print(f"  First direction: {direction}")
    print(f"  Transaction: {first_transfer.get('transaction_hash')}")
    print(f"  Incoming transfers: {len(incoming)}")
    print(f"  Outgoing transfers: {len(outgoing)}")
    print(f"  Current recorded balance: {token.get('balance')}")

    if outgoing:
        print("  Status: Wallet has moved or sold some tokens.")
    else:
        print("  Status: No outgoing token transfer found.")

    print(
        "  Acquisition type: Not classified yet — "
        "transaction inspection required."
    )


def main():
    candidates = load_candidates()
    wallet, information = choose_wallet(candidates)

    print(f"\nSelected wallet: {wallet}")

    transfers = fetch_wallet_transfers(wallet)

    if not transfers:
        print("No token-transfer history was retrieved.")
        return

    for token in information.get("tokens", []):
        analyse_token(wallet, token, transfers)


if __name__ == "__main__":
    main()