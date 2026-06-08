"""Steam Community Market operations: prices, listings, orders and history."""

from __future__ import annotations

import json
import urllib.parse
from decimal import Decimal
from http import HTTPStatus
from typing import TYPE_CHECKING

from steampy.confirmation import ConfirmationExecutor
from steampy.exceptions import ApiException, TooManyRequests
from steampy.models import Currency, GameOptions, SteamUrl
from steampy.utils import (
    get_history_id_to_assets_address_from_html,
    get_listing_id_to_assets_address_from_html,
    get_market_history_from_api,
    get_market_listings_from_html,
    get_market_sell_listings_from_api,
    login_required,
    merge_items_with_descriptions_from_history,
    merge_items_with_descriptions_from_listing,
    retry_call,
    text_between,
)

if TYPE_CHECKING:
    import requests

    from steampy.session import RotatingProxySession

# Steam paginates market render endpoints in pages of this size.
_MARKET_PAGE_SIZE = 100
# Above this many listings, Steam only serves them through the paged endpoint.
_RENDER_PAGE_THRESHOLD = 1000


def market_listing_referer(game: GameOptions, market_name: str) -> str:
    """Build the Referer header Steam expects for buy/sell market requests."""
    quoted_name = urllib.parse.quote(market_name)
    return f"{SteamUrl.COMMUNITY_URL}/market/listings/{game.app_id}/{quoted_name}"


def build_buy_order_data(
    session_id: str,
    market_name: str,
    price_single_item: str,
    quantity: int,
    game: GameOptions,
    currency: Currency,
) -> dict:
    """Build the POST body shared by the sync and async buy-order requests."""
    return {
        "sessionid": session_id,
        "currency": currency.value,
        "appid": game.app_id,
        "market_hash_name": market_name,
        "price_total": str(Decimal(price_single_item) * Decimal(quantity)),
        "quantity": quantity,
    }


def require_ok(response: requests.Response, context: str) -> None:
    """Raise :class:`ApiException` unless ``response`` has an HTTP 200 status."""
    if response.status_code != HTTPStatus.OK:
        raise ApiException(f"{context}. HTTP code: {response.status_code}")


class SteamMarket:
    """Market actions performed with the authenticated client's session."""

    def __init__(self, session: RotatingProxySession) -> None:
        self._session = session
        self._steam_guard: dict = {}
        self._session_id: str = ""
        self.was_login_executed = False

    def _set_login_executed(self, steamguard: dict, session_id: str) -> None:
        self._steam_guard = steamguard
        self._session_id = session_id
        self.was_login_executed = True

    def _fetch_market_json(self, url: str, params: dict) -> dict:
        """GET a market JSON endpoint, mapping HTTP 429 to ``TooManyRequests``."""
        response = self._session.rotating_get(url, params=params)
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise TooManyRequests("You can fetch maximum 20 prices in 60s period")
        return response.json()

    def fetch_price(
        self,
        item_hash_name: str,
        game: GameOptions,
        currency: Currency = Currency.USD,
        country: str = "PL",
    ) -> dict:
        """Fetch the current price overview for a single market item."""
        params = {
            "country": country,
            "currency": currency.value,
            "appid": game.app_id,
            "market_hash_name": item_hash_name,
        }
        return self._fetch_market_json(
            f"{SteamUrl.COMMUNITY_URL}/market/priceoverview/", params
        )

    @login_required
    def fetch_price_history(self, item_hash_name: str, game: GameOptions) -> dict:
        """Fetch the historical price series for a single market item."""
        params = {
            "country": "PL",
            "appid": game.app_id,
            "market_hash_name": item_hash_name,
        }
        return self._fetch_market_json(
            f"{SteamUrl.COMMUNITY_URL}/market/pricehistory/", params
        )

    @login_required
    def get_my_market_listings(self) -> dict:
        """Return the account's buy orders and sell listings (all pages)."""
        response = self._session.rotating_get(f"{SteamUrl.COMMUNITY_URL}/market")
        require_ok(response, "There was a problem getting the listings")

        assets_descriptions = json.loads(
            text_between(response.text, "var g_rgAssets = ", ";\n")
        )
        listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(
            response.text
        )
        listings = get_market_listings_from_html(response.text)
        listings = merge_items_with_descriptions_from_listing(
            listings,
            listing_id_to_assets_address,
            assets_descriptions,
        )

        if '<span id="tabContentsMyActiveMarketListings_end">' in response.text:
            n_showing = int(
                text_between(
                    response.text,
                    '<span id="tabContentsMyActiveMarketListings_end">',
                    "</span>",
                )
            )
            n_total = int(
                text_between(
                    response.text,
                    '<span id="tabContentsMyActiveMarketListings_total">',
                    "</span>",
                ).replace(",", ""),
            )
            self._fetch_remaining_listings(listings, n_showing, n_total)

        return listings

    def _fetch_remaining_listings(
        self, listings: dict, n_showing: int, n_total: int
    ) -> None:
        """Fetch and merge the sell listings beyond the first market page."""
        if n_showing < n_total < _RENDER_PAGE_THRESHOLD:
            url = (
                f"{SteamUrl.COMMUNITY_URL}/market/mylistings/render/"
                f"?query=&start={n_showing}&count=-1"
            )
            response = self._session.rotating_get(url)
            require_ok(response, "There was a problem getting the listings")
            self._merge_render_listings(listings, response.json())
        else:
            for start in range(n_showing, n_total, _MARKET_PAGE_SIZE):
                url = (
                    f"{SteamUrl.COMMUNITY_URL}/market/mylistings/"
                    f"?query=&start={start}&count={_MARKET_PAGE_SIZE}"
                )
                response = self._session.rotating_get(url)
                require_ok(response, "There was a problem getting the listings")
                self._merge_render_listings(listings, response.json())

    @staticmethod
    def _merge_render_listings(listings: dict, jresp: dict) -> None:
        id_to_address = get_listing_id_to_assets_address_from_html(
            jresp.get("hovers", "")
        )
        page = get_market_sell_listings_from_api(jresp.get("results_html", ""))
        page = merge_items_with_descriptions_from_listing(
            page, id_to_address, jresp.get("assets", {})
        )
        listings["sell_listings"] = {
            **listings["sell_listings"],
            **page["sell_listings"],
        }

    @login_required
    def get_market_history(self, max_retries: int = 20) -> dict:
        """Return the account's full market transaction history.

        Steam frequently rate-limits this endpoint, so each page is retried up
        to ``max_retries`` times before giving up.
        """
        url = f"{SteamUrl.COMMUNITY_URL}/market/myhistory/render/"

        def query(params: dict) -> dict:
            response = retry_call(
                lambda: self._session.rotating_get(url, params=params),
                attempts=max_retries,
                retry_if=lambda r: r.status_code != HTTPStatus.OK,
            )
            require_ok(response, "There was a problem getting the market history")
            return response.json()

        total_count = query({"start": 0, "count": 1}).get("total_count", 0)

        history: dict = {}
        for start in range(0, total_count + _MARKET_PAGE_SIZE, _MARKET_PAGE_SIZE):
            jresp = query({"start": start, "count": _MARKET_PAGE_SIZE})
            address = get_history_id_to_assets_address_from_html(
                jresp.get("hovers", "")
            )
            page = get_market_history_from_api(jresp.get("results_html", ""))
            history.update(
                merge_items_with_descriptions_from_history(
                    page, address, jresp.get("assets", {})
                )
            )
        return history

    @login_required
    def create_sell_order(
        self,
        assetid: str,
        game: GameOptions,
        money_to_receive: str,
        amount: int = 1,
    ) -> dict:
        """List ``amount`` units of an inventory item for sale."""
        data = {
            "assetid": assetid,
            "sessionid": self._session_id,
            "contextid": game.context_id,
            "appid": game.app_id,
            "amount": amount,
            "price": money_to_receive,
        }
        steamid = self._steam_guard["steamid"]
        headers = {"Referer": f"{SteamUrl.COMMUNITY_URL}/profiles/{steamid}/inventory"}

        response = self._session.rotating_post(
            f"{SteamUrl.COMMUNITY_URL}/market/sellitem/",
            data=data,
            headers=headers,
        ).json()
        has_pending_confirmation = "pending confirmation" in response.get("message", "")
        needs_confirmation = response.get("needs_mobile_confirmation") or (
            not response.get("success") and has_pending_confirmation
        )
        # Cookie-only sessions have no identity_secret; in that case the listing
        # is created but must be confirmed manually in the Steam mobile app.
        if needs_confirmation and "identity_secret" in self._steam_guard:
            return self._confirm_sell_listing(assetid)
        return response

    @login_required
    def create_buy_order(
        self,
        market_name: str,
        price_single_item: str,
        quantity: int,
        game: GameOptions,
        currency: Currency = Currency.USD,
    ) -> dict:
        """Place a buy order for ``quantity`` units of a market item."""
        data = build_buy_order_data(
            self._session_id, market_name, price_single_item, quantity, game, currency
        )
        headers = {"Referer": market_listing_referer(game, market_name)}
        response = self._session.rotating_post(
            f"{SteamUrl.COMMUNITY_URL}/market/createbuyorder/",
            data=data,
            headers=headers,
        ).json()
        if (success := response.get("success")) != 1:
            raise ApiException(
                "There was a problem creating the order. "
                f"Are you using the right currency? success: {success}",
            )
        return response

    @login_required
    def buy_item(
        self,
        market_name: str,
        market_id: str,
        price: int,
        fee: int,
        game: GameOptions,
        currency: Currency = Currency.USD,
    ) -> dict:
        """Buy a specific market listing identified by ``market_id``."""
        data = {
            "sessionid": self._session_id,
            "currency": currency.value,
            "subtotal": price - fee,
            "fee": fee,
            "total": price,
            "quantity": "1",
        }
        headers = {"Referer": market_listing_referer(game, market_name)}
        response = self._session.rotating_post(
            f"{SteamUrl.COMMUNITY_URL}/market/buylisting/{market_id}",
            data=data,
            headers=headers,
        ).json()

        try:
            success = response["wallet_info"]["success"]
        except (KeyError, TypeError) as exc:
            message = response.get("message")
            raise ApiException(
                f"There was a problem buying this item. Message: {message}",
            ) from exc
        if success != 1:
            raise ApiException(
                "There was a problem buying this item. "
                f"Are you using the right currency? success: {success}",
            )
        return response

    @login_required
    def cancel_sell_order(self, sell_listing_id: str) -> None:
        """Remove one of the account's sell listings."""
        data = {"sessionid": self._session_id}
        headers = {"Referer": f"{SteamUrl.COMMUNITY_URL}/market/"}
        url = f"{SteamUrl.COMMUNITY_URL}/market/removelisting/{sell_listing_id}"
        response = self._session.rotating_post(url, data=data, headers=headers)
        require_ok(response, "There was a problem removing the listing")

    @login_required
    def cancel_buy_order(self, buy_order_id: str) -> dict:
        """Cancel one of the account's buy orders."""
        data = {"sessionid": self._session_id, "buy_orderid": buy_order_id}
        headers = {"Referer": f"{SteamUrl.COMMUNITY_URL}/market"}
        response = self._session.rotating_post(
            f"{SteamUrl.COMMUNITY_URL}/market/cancelbuyorder/",
            data=data,
            headers=headers,
        ).json()
        if (success := response.get("success")) != 1:
            raise ApiException(
                f"There was a problem canceling the order. success: {success}"
            )
        return response

    def _confirm_sell_listing(self, asset_id: str) -> dict:
        executor = ConfirmationExecutor(
            self._steam_guard["identity_secret"],
            self._steam_guard["steamid"],
            self._session,
        )
        return executor.confirm_sell_listing(asset_id)
