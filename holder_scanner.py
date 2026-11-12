import requests

TOKEN_CONTRACT = "0x57C0E45cB534413D1C20A4240955d6bB250BB4F1"
URL = f"https://robinhoodchain.blockscout.com/api/v2/tokens/{TOKEN_CONTRACT}/holders"

response = requests.get(URL, timeout=30)
response.raise_for_status()

data = response.json()
holders = data.get("items", [])

wallets = []

for holder in holders:
    address = holder.get("address", {})

    if address.get("is_contract") is False:
        wallets.append(holder)

print(f"Total holders returned: {len(holders)}")
print(f"Normal wallets found: {len(wallets)}")
print("-" * 80)

for holder in wallets:
    address = holder.get("address", {})
    wallet_hash = address.get("hash")
    balance = holder.get("value")
    is_scam = address.get("is_scam")

    print(
        f"Wallet: {wallet_hash} | "
        f"Balance: {balance} | "
        f"Scam flag: {is_scam}"
    )