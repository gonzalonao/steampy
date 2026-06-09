"""Concurrent Steam Market operations built on top of ``aiohttp``.

This is a thin asynchronous layer: request construction is delegated to the same
payload builders used by the synchronous :class:`steampy.market.SteamMarket`, so
only the transport is asynchronous. Install the optional dependency with
``pip install steampy[async]``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from steampy.market import build_buy_order_data, market_listing_referer
from steampy.models import Currency, GameOptions, SteamUrl

try:
    import aiohttp
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    raise ModuleNotFoundError(
        "AsyncMarket requires the optional 'aiohttp' dependency. "
        "Install it with: pip install steampy[async]",
    ) from exc

if TYPE_CHECKING:
    from steampy.client import SteamClient

# A buy order placed concurrently is described by its name, unit price and size.
BuyOrder = tuple[str, str, int]


def _proxy_url(proxy: dict | None) -> str | None:
    """Extract a single proxy URL from a ``requests``-style proxy dict."""
    if not proxy:
        return None
    return proxy.get("https") or proxy.get("http")


class AsyncMarket:
    """Place market requests concurrently for one authenticated account."""

    def __init__(
        self, session_id: str, cookies: dict, proxy: dict | None = None
    ) -> None:
        self._session_id = session_id
        self._cookies = cookies
        self._proxy = _proxy_url(proxy)

    @classmethod
    def from_client(cls, client: SteamClient, proxy: dict | None = None) -> AsyncMarket:
        """Build an :class:`AsyncMarket` from a logged-in :class:`SteamClient`."""
        cookies = client._session.cookies.get_dict(domain="steamcommunity.com")
        return cls(client._get_session_id(), cookies, proxy)

    async def fetch_prices(
        self,
        item_hash_names: list[str],
        game: GameOptions,
        currency: Currency = Currency.USD,
        country: str = "PL",
    ) -> dict[str, dict]:
        """Fetch price overviews for many items concurrently.

        Returns:
            A mapping of each item hash name to its price-overview payload.
        """
        async with aiohttp.ClientSession(cookies=self._cookies) as session:
            tasks = [
                self._fetch_price(session, name, game, currency, country)
                for name in item_hash_names
            ]
            results = await asyncio.gather(*tasks)
        return dict(zip(item_hash_names, results, strict=True))

    async def create_buy_orders(
        self,
        orders: list[BuyOrder],
        game: GameOptions,
        currency: Currency = Currency.USD,
    ) -> list[dict]:
        """Place several buy orders concurrently.

        Args:
            orders: ``(market_hash_name, price_single_item, quantity)`` tuples.
            game: The game the items belong to.
            currency: The wallet currency to price the orders in.

        Returns:
            The Steam JSON response for each order, in the input order.
        """
        async with aiohttp.ClientSession(cookies=self._cookies) as session:
            tasks = [
                self._create_buy_order(session, order, game, currency)
                for order in orders
            ]
            return list(await asyncio.gather(*tasks))

    async def _fetch_price(
        self,
        session: aiohttp.ClientSession,
        item_hash_name: str,
        game: GameOptions,
        currency: Currency,
        country: str,
    ) -> dict:
        params = {
            "country": country,
            "currency": currency.value,
            "appid": game.app_id,
            "market_hash_name": item_hash_name,
        }
        url = f"{SteamUrl.COMMUNITY_URL}/market/priceoverview/"
        async with session.get(url, params=params, proxy=self._proxy) as response:
            return await response.json()

    async def _create_buy_order(
        self,
        session: aiohttp.ClientSession,
        order: BuyOrder,
        game: GameOptions,
        currency: Currency,
    ) -> dict:
        market_name, price_single_item, quantity = order
        data = build_buy_order_data(
            self._session_id, market_name, price_single_item, quantity, game, currency
        )
        headers = {"Referer": market_listing_referer(game, market_name)}
        url = f"{SteamUrl.COMMUNITY_URL}/market/createbuyorder/"
        async with session.post(
            url, data=data, headers=headers, proxy=self._proxy
        ) as response:
            return await response.json()
