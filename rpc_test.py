import requests


RPC_URL = "https://rpc.mainnet.chain.robinhood.com"


def rpc_call(method, params=None):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or [],
    }

    response = requests.post(
        RPC_URL,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()

    if "error" in data:
        raise RuntimeError(data["error"])

    return data["result"]


latest_block_hex = rpc_call("eth_blockNumber")
latest_block = int(latest_block_hex, 16)

chain_id_hex = rpc_call("eth_chainId")
chain_id = int(chain_id_hex, 16)

print(f"Connected successfully")
print(f"Chain ID: {chain_id}")
print(f"Latest block: {latest_block:,}")