"""Bootstrap several cookie-authenticated clients behind a shared proxy pool."""

from steampy.accounts import fetch_proxies, load_clients

# Build a validated proxy pool from a URL serving ip:port:user:password lines.
proxies = fetch_proxies("https://example.com/proxies.txt")

# Each account is identified by a name and its login-cookie dict.
accounts = {
    "main": {"sessionid": "...", "steamLoginSecure": "...", "steamCountry": "..."},
    "alt": {"sessionid": "...", "steamLoginSecure": "...", "steamCountry": "..."},
}

clients = load_clients(accounts, proxies=proxies)
for name, client in clients.items():
    listings = client.market.get_my_market_listings()
    print(f"{name}: {len(listings['sell_listings'])} active sell listings")
