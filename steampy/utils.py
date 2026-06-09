"""Shared helpers: parsing, id conversion, pricing, retries and proxies."""

from __future__ import annotations

import copy
import math
import re
import struct
import time
from collections.abc import Callable
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Concatenate, Protocol, cast
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.structures import CaseInsensitiveDict

from steampy.exceptions import LoginRequired

if TYPE_CHECKING:
    from steampy.models import GameOptions

# Steam encodes the 32-bit account id into the 64-bit steam id with this mask.
_STEAM_ID_ACCOUNT_MASK = 0x1100001

# Regexes that extract ids and asset addresses from the market HTML/JS payloads.
_SELL_LISTING_ID_RE = re.compile(r"mylisting_(\d+)")
_BUY_ORDER_ID_RE = re.compile(r"mybuyorder_\d+")
_HISTORY_ROW_ID_RE = re.compile(r"history_row_\d+")
_LISTING_HOVER_RE = (
    r"CreateItemHoverFromContainer\( [\w]+, 'mylisting_([\d]+)_[\w]+', "
    r"([\d]+), '([\d]+)', '([\d]+)', [\d]+ \);"
)
_HISTORY_HOVER_RE = (
    r"CreateItemHoverFromContainer\( [\w]+, 'history_row_([\d]+)_([\d]+)_[\w]+', "
    r"([\d]+), '([\d]+)', '([\d]+)', [\d]+ \);"
)


class _Authenticated(Protocol):
    """Anything exposing the login flag checked by :func:`login_required`."""

    was_login_executed: bool


def login_required[S: _Authenticated, **P, R](
    func: Callable[Concatenate[S, P], R],
) -> Callable[Concatenate[S, P], R]:
    """Guard a method so it only runs once login has been executed."""

    @wraps(func)
    def wrapper(self: S, *args: P.args, **kwargs: P.kwargs) -> R:
        if not self.was_login_executed:
            raise LoginRequired("Use login method first")
        return func(self, *args, **kwargs)

    return cast("Callable[Concatenate[S, P], R]", wrapper)


def retry_call[T](
    action: Callable[[], T],
    *,
    attempts: int,
    retry_exceptions: tuple[type[Exception], ...] = (),
    retry_if: Callable[[T], bool] | None = None,
    delay: float = 0.0,
) -> T:
    """Invoke ``action`` repeatedly until it succeeds or attempts run out.

    A call is retried when it raises one of ``retry_exceptions`` or when
    ``retry_if`` returns ``True`` for its result. The final result is returned
    even if it still matches ``retry_if``; if every attempt raised, the last
    exception is re-raised.

    Args:
        action: The zero-argument callable to invoke.
        attempts: Maximum number of attempts (must be at least one).
        retry_exceptions: Exception types that trigger a retry.
        retry_if: Predicate over the result that triggers a retry.
        delay: Seconds to sleep between attempts.

    Returns:
        The result of the last attempt.

    Raises:
        ValueError: If ``attempts`` is not positive.
        Exception: The last exception raised when no attempt produced a result.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    last_exception: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = action()
        except retry_exceptions as exc:
            last_exception = exc
            if attempt < attempts and delay:
                time.sleep(delay)
            continue

        if retry_if is not None and retry_if(result) and attempt < attempts:
            if delay:
                time.sleep(delay)
            continue
        return result

    assert last_exception is not None  # only reachable when every attempt raised
    raise last_exception


def text_between(text: str, begin: str, end: str) -> str:
    """Return the substring of ``text`` between the first ``begin`` and ``end``."""
    start = text.index(begin) + len(begin)
    stop = text.index(end, start)
    return text[start:stop]


def texts_between(text: str, begin: str, end: str) -> list[str]:
    """Return every substring of ``text`` delimited by ``begin`` and ``end``."""
    results = []
    stop = 0
    while True:
        try:
            start = text.index(begin, stop) + len(begin)
            stop = text.index(end, start)
            results.append(text[start:stop])
        except ValueError:
            return results


def account_id_to_steam_id(account_id: str) -> str:
    """Convert a 32-bit account id into its 64-bit steam id."""
    first_bytes = int(account_id).to_bytes(4, byteorder="big")
    last_bytes = _STEAM_ID_ACCOUNT_MASK.to_bytes(4, byteorder="big")
    return str(struct.unpack(">Q", last_bytes + first_bytes)[0])


def steam_id_to_account_id(steam_id: str) -> str:
    """Convert a 64-bit steam id into its 32-bit account id."""
    return str(struct.unpack(">L", int(steam_id).to_bytes(8, byteorder="big")[4:])[0])


def clean_price(raw: str) -> str:
    """Normalise a localized Steam price string into a plain decimal string.

    Strips currency symbols and grouping separators and converts a decimal
    comma into a dot, so ``'1.234,56 €'`` becomes ``'1234.56'`` and an empty
    ``'-'`` price becomes ``'0'``.
    """
    text = raw.strip().replace("\xa0", "").replace("-", "0")
    text = re.sub(r"[^\d.,]", "", text)
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    return text or "0"


def calculate_gross_price(
    price_net: Decimal,
    publisher_fee: Decimal,
    steam_fee: Decimal = Decimal("0.05"),
) -> Decimal:
    """Calculate the price including the publisher's fee and the Steam fee.

    Args:
        price_net: The amount the seller receives after a market transaction.
        publisher_fee: The game-specific publisher fee (commonly ``0.10``).
        steam_fee: The Steam transaction fee (currently ``0.05``).

    Returns:
        The gross price (including fees) that the buyer pays.
    """
    price_net *= 100
    steam_fee_amount = math.floor(max(price_net * steam_fee, Decimal(1)))
    publisher_fee_amount = math.floor(max(price_net * publisher_fee, Decimal(1)))
    price_gross = price_net + steam_fee_amount + publisher_fee_amount
    return Decimal(price_gross) / 100


def calculate_net_price(
    price_gross: Decimal,
    publisher_fee: Decimal,
    steam_fee: Decimal = Decimal("0.05"),
) -> Decimal:
    """Calculate the seller's net price from a gross price.

    Args:
        price_gross: The amount the buyer pays during a market transaction.
        publisher_fee: The game-specific publisher fee (commonly ``0.10``).
        steam_fee: The Steam transaction fee (currently ``0.05``).

    Returns:
        The net price (without fees) that the seller receives.
    """
    price_gross *= 100
    estimated_net_price = Decimal(int(price_gross / (steam_fee + publisher_fee + 1)))
    estimated_gross_price = (
        calculate_gross_price(estimated_net_price / 100, publisher_fee, steam_fee) * 100
    )

    # calculate_gross_price floors its fees, so the estimate can be a cent or
    # two off; nudge it until the round-trip matches (bounded to stay safe).
    max_corrections = 10
    ever_undershot = False
    corrections = 0
    while estimated_gross_price != price_gross and corrections < max_corrections:
        if estimated_gross_price > price_gross:
            if ever_undershot:
                break
            estimated_net_price -= 1
        else:
            ever_undershot = True
            estimated_net_price += 1

        estimated_gross_price = (
            calculate_gross_price(estimated_net_price / 100, publisher_fee, steam_fee)
            * 100
        )
        corrections += 1
    return estimated_net_price / 100


def merge_items_with_descriptions_from_inventory(
    inventory_response: dict, game: GameOptions
) -> dict:
    """Attach descriptions to the assets of a modern inventory response."""
    inventory = inventory_response.get("assets", [])
    if not inventory:
        return {}
    descriptions = {
        get_description_key(description): description
        for description in inventory_response["descriptions"]
    }
    return merge_items(inventory, descriptions, context_id=game.context_id)


def merge_items_with_descriptions_from_offers(offers_response: dict) -> dict:
    """Attach descriptions to every item in a trade-offers response."""
    descriptions = {
        get_description_key(offer): offer
        for offer in offers_response["response"].get("descriptions", [])
    }
    received_offers = offers_response["response"].get("trade_offers_received", [])
    sent_offers = offers_response["response"].get("trade_offers_sent", [])
    offers_response["response"]["trade_offers_received"] = [
        merge_items_with_descriptions_from_offer(offer, descriptions)
        for offer in received_offers
    ]
    offers_response["response"]["trade_offers_sent"] = [
        merge_items_with_descriptions_from_offer(offer, descriptions)
        for offer in sent_offers
    ]
    return offers_response


def merge_items_with_descriptions_from_offer(offer: dict, descriptions: dict) -> dict:
    """Attach descriptions to the items of a single trade offer."""
    offer["items_to_give"] = merge_items(offer.get("items_to_give", []), descriptions)
    offer["items_to_receive"] = merge_items(
        offer.get("items_to_receive", []), descriptions
    )
    return offer


def merge_items_with_descriptions_from_listing(
    listings: dict,
    ids_to_assets_address: dict,
    descriptions: dict,
) -> dict:
    """Attach descriptions to market sell listings via their asset addresses."""
    for listing_id, listing in listings["sell_listings"].items():
        asset_address = ids_to_assets_address[listing_id]
        description = descriptions[asset_address[0]][asset_address[1]][asset_address[2]]
        listing["description"] = description
    return listings


def merge_items_with_descriptions_from_history(
    history: dict,
    ids_to_assets_address: dict,
    descriptions: dict,
) -> dict:
    """Attach descriptions to market history rows via their asset addresses."""
    for history_id, history_item in history.items():
        asset_address = ids_to_assets_address[history_id]
        description = descriptions[asset_address[0]][asset_address[1]][asset_address[2]]
        history_item["description"] = description
    return history


def merge_items(items: list[dict], descriptions: dict, **kwargs: str) -> dict:
    """Index ``items`` by id, enriching each with a copy of its description."""
    merged_items = {}
    for item in items:
        description_key = get_description_key(item)
        description = copy.copy(descriptions[description_key])
        item_id = item.get("id") or item["assetid"]
        description["contextid"] = item.get("contextid") or kwargs["context_id"]
        description["id"] = item_id
        description["amount"] = item["amount"]
        merged_items[item_id] = description
    return merged_items


def get_market_listings_from_html(html: str) -> dict:
    """Parse the market home page into buy orders and sell listings."""
    document = BeautifulSoup(html, "html.parser")
    nodes = document.select("div[id=myListings]")[0].find_all(
        "div", {"class": "market_home_listing_table"}
    )
    sell_listings_dict: dict = {}
    buy_orders_dict: dict = {}

    for node in nodes:
        if "My sell listings" in node.text:
            sell_listings_dict = get_sell_listings_from_node(node)
        elif "My listings awaiting confirmation" in node.text:
            sell_listings_awaiting_conf = get_sell_listings_from_node(node)
            for listing in sell_listings_awaiting_conf.values():
                listing["need_confirmation"] = True
            sell_listings_dict.update(sell_listings_awaiting_conf)
        elif "My buy orders" in node.text:
            buy_orders_dict = get_buy_orders_from_node(node)

    return {"buy_orders": buy_orders_dict, "sell_listings": sell_listings_dict}


def get_sell_listings_from_node(node: Tag) -> dict:
    """Parse the sell-listing rows contained in a market table node."""
    sell_listings_raw = node.find_all("div", {"id": _SELL_LISTING_ID_RE})
    sell_listings_dict = {}
    for listing_raw in sell_listings_raw:
        spans = listing_raw.select("span[title]")
        listing = {
            "listing_id": str(listing_raw.attrs["id"]).replace("mylisting_", ""),
            "buyer_pay": spans[0].text.strip(),
            "you_receive": spans[1].text.strip()[1:-1],
            "created_on": listing_raw.find_all(
                "div", {"class": "market_listing_listed_date"}
            )[0].text.strip(),
            "need_confirmation": False,
        }
        sell_listings_dict[listing["listing_id"]] = listing
    return sell_listings_dict


def get_market_sell_listings_from_api(html: str) -> dict:
    """Parse sell listings from a market render-API HTML fragment."""
    document = BeautifulSoup(html, "html.parser")
    return {"sell_listings": get_sell_listings_from_node(document)}


def get_buy_orders_from_node(node: Tag) -> dict:
    """Parse the buy-order rows contained in a market table node."""
    buy_orders_raw = node.find_all("div", {"id": _BUY_ORDER_ID_RE})
    buy_orders_dict = {}
    for order in buy_orders_raw:
        qnt_price_raw = order.select("span[class=market_listing_price]")[0].text.split(
            "@"
        )
        item_anchor = order.select_one("a")
        icon_src = str(
            order.select("img[class=market_listing_item_img]")[0].attrs["src"]
        )
        order_data = {
            "order_id": str(order.attrs["id"]).replace("mybuyorder_", ""),
            "quantity": int(qnt_price_raw[0].strip()),
            "price": clean_price(qnt_price_raw[1]),
            "item_name": item_anchor.text if item_anchor else "",
            "icon_url": icon_src.rsplit("/", 2)[-2],
            "game_name": order.select("span[class=market_listing_game_name]")[0].text,
        }
        buy_orders_dict[order_data["order_id"]] = order_data
    return buy_orders_dict


def get_market_history_from_node(node: Tag) -> dict:
    """Parse the rows of the market transaction-history page."""
    history_raw = node.find_all("div", {"id": _HISTORY_ROW_ID_RE})
    history_dict = {}
    for row in history_raw:
        gainorloss = row.find_all("div", {"class": "market_listing_gainorloss"})[
            0
        ].text.strip()
        history_item = {
            "history_id": str(row.attrs["id"]).replace("history_row_", ""),
            "sale_type": "1" if gainorloss == "+" else "0",
            "price": clean_price(row.select("span.market_listing_price")[0].text),
            "display_name": row.select("span.market_listing_item_name")[0].text.strip(),
            "date": row.find_all("div", {"class": "market_listing_listed_date"})[
                0
            ].text.strip(),
        }
        history_dict[history_item["history_id"]] = history_item
    return history_dict


def get_market_history_from_api(html: str) -> dict:
    """Parse market history rows from a market render-API HTML fragment."""
    document = BeautifulSoup(html, "html.parser")
    return get_market_history_from_node(document)


def get_listing_id_to_assets_address_from_html(html: str) -> dict:
    """Map each sell-listing id to its ``[appid, contextid, assetid]`` address."""
    listing_id_to_assets_address = {}
    for match in re.findall(_LISTING_HOVER_RE, html):
        listing_id_to_assets_address[match[0]] = [str(match[1]), match[2], match[3]]
    return listing_id_to_assets_address


def get_history_id_to_assets_address_from_html(html: str) -> dict:
    """Map each history-row id to its ``[appid, contextid, assetid]`` address."""
    history_id_to_assets_address = {}
    for match in re.findall(_HISTORY_HOVER_RE, html):
        key = f"{match[0]}_{match[1]}"
        history_id_to_assets_address[key] = [str(match[2]), match[3], match[4]]
    return history_id_to_assets_address


def get_description_key(item: dict) -> str:
    """Build the ``classid_instanceid`` key used to look up a description."""
    return f"{item['classid']}_{item['instanceid']}"


def get_key_value_from_url(url: str, key: str, case_sensitive: bool = True) -> str:
    """Extract a single query-string value from ``url``."""
    params = urlparse(url).query
    if case_sensitive:
        return parse_qs(params)[key][0]
    return CaseInsensitiveDict(parse_qs(params))[key][0]


class Credentials:
    """A single account's login, password and API key."""

    def __init__(self, login: str, password: str, api_key: str) -> None:
        self.login = login
        self.password = password
        self.api_key = api_key


def load_credentials() -> list[Credentials]:
    """Load whitespace-separated credentials from ``secrets/credentials.pwd``."""
    dirname = Path(__file__).resolve().parent
    credentials = []
    with Path(f"{dirname}/../secrets/credentials.pwd").open(encoding="utf-8") as f:
        for line in f:
            login, password, api_key = line.split()[:3]
            credentials.append(Credentials(login, password, api_key))
    return credentials


def ping_proxy(proxies: dict) -> bool:
    """Return whether ``proxies`` can reach the Steam community site."""
    try:
        requests.get("https://steamcommunity.com/", proxies=proxies, timeout=10)
    except requests.RequestException:
        return False
    return True


def create_cookie(name: str, cookie: str, domain: str) -> dict:
    """Build a cookie dict accepted by :meth:`requests.cookies.set`."""
    return {"name": name, "value": cookie, "domain": domain}
