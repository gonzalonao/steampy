"""Place many market buy orders concurrently with AsyncMarket.

Requires the optional async extra: ``pip install steampy[async]``.
"""

import asyncio

from steampy.async_market import AsyncMarket
from steampy.client import SteamClient
from steampy.models import Currency, GameOptions

api_key = ""
steam_guard_path = ""
username = ""
password = ""


async def main() -> None:
    client = SteamClient(api_key)
    client.login(username, password, steam_guard_path)

    market = AsyncMarket.from_client(client)
    orders = [
        ("AK-47 | Redline (Field-Tested)", "10.00", 1),
        ("AWP | Asiimov (Field-Tested)", "30.00", 1),
    ]
    responses = await market.create_buy_orders(orders, GameOptions.CS, Currency.USD)
    for order, response in zip(orders, responses, strict=True):
        print(f"{order[0]}: {response}")


if __name__ == "__main__":
    asyncio.run(main())
