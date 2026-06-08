"""Drive the client through a rotating pool of proxies.

Pass a list of proxy dicts and steampy round-robins over them for every
market and inventory request, automatically failing over to the next proxy
when one stops responding.
"""

from steampy.client import SteamClient
from steampy.models import GameOptions

api_key = ""
steam_guard_path = ""
username = ""
password = ""

# Each proxy is a regular requests-style dict; pass a list to enable rotation.
proxies = [
    {"http": "http://user:pass@host1:port", "https": "http://user:pass@host1:port"},
    {"http": "http://user:pass@host2:port", "https": "http://user:pass@host2:port"},
]

client = SteamClient(api_key, proxies=proxies)
client.login(username, password, steam_guard_path)

# Every request below transparently rotates through the proxy pool.
inventory = client.get_my_inventory(GameOptions.CS)
print(f"Fetched {len(inventory)} inventory items through the proxy pool")
