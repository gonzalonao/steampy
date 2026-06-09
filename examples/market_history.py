"""Download the full Steam Market transaction history of an account."""

from steampy.client import SteamClient

api_key = ""
steam_guard_path = ""
username = ""
password = ""

client = SteamClient(api_key)
client.login(username, password, steam_guard_path)

# Paginates through every page and retries the rate-limited endpoint.
history = client.market.get_market_history()
print(f"Fetched {len(history)} market transactions")
for transaction in list(history.values())[:5]:
    side = "bought" if transaction["sale_type"] == "0" else "sold"
    name = transaction["display_name"]
    print(f"{transaction['date']}: {side} {name} for {transaction['price']}")
