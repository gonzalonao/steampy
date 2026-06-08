from bs4 import BeautifulSoup

from steampy.market import build_buy_order_data, market_listing_referer
from steampy.models import Currency, GameOptions
from steampy.utils import (
    get_buy_orders_from_node,
    get_history_id_to_assets_address_from_html,
    get_listing_id_to_assets_address_from_html,
    get_market_history_from_node,
    get_sell_listings_from_node,
)

SELL_LISTINGS_HTML = """
<div id="mylisting_111">
  <span title="buyer">$1.00</span>
  <span title="receive">($0.87)</span>
  <div class="market_listing_listed_date">5 Jun</div>
</div>
"""

BUY_ORDERS_HTML = """
<div id="mybuyorder_222">
  <span class="market_listing_price">3 @ &#8364;1,50</span>
  <a>AK-47 | Redline</a>
  <img class="market_listing_item_img" src="https://cdn/abc/iconhash/96fx96f"/>
  <span class="market_listing_game_name">Counter-Strike 2</span>
</div>
"""

HISTORY_HTML = """
<div id="history_row_333_444">
  <div class="market_listing_gainorloss">+</div>
  <span class="market_listing_price">&#8364;2,00</span>
  <span class="market_listing_item_name">Glock</span>
  <div class="market_listing_listed_date">6 Jun</div>
</div>
"""


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def test_get_sell_listings_from_node() -> None:
    listings = get_sell_listings_from_node(_soup(SELL_LISTINGS_HTML))
    assert "111" in listings
    listing = listings["111"]
    assert listing["buyer_pay"] == "$1.00"
    assert listing["you_receive"] == "$0.87"
    assert listing["need_confirmation"] is False


def test_get_buy_orders_from_node() -> None:
    orders = get_buy_orders_from_node(_soup(BUY_ORDERS_HTML))
    assert "222" in orders
    order = orders["222"]
    assert order["quantity"] == 3
    assert order["price"] == "1.50"  # locale-normalised
    assert order["item_name"] == "AK-47 | Redline"
    assert order["icon_url"] == "iconhash"
    assert order["game_name"] == "Counter-Strike 2"


def test_get_market_history_from_node() -> None:
    history = get_market_history_from_node(_soup(HISTORY_HTML))
    assert "333_444" in history
    item = history["333_444"]
    assert item["sale_type"] == "1"  # gain
    assert item["price"] == "2.00"
    assert item["display_name"] == "Glock"


def test_listing_id_to_assets_address() -> None:
    html = (
        "CreateItemHoverFromContainer( g, 'mylisting_123_image', 730, '2', '456', 0 );"
    )
    assert get_listing_id_to_assets_address_from_html(html) == {
        "123": ["730", "2", "456"]
    }


def test_history_id_to_assets_address() -> None:
    html = (
        "CreateItemHoverFromContainer( g, 'history_row_333_444_image', "
        "730, '2', '456', 0 );"
    )
    assert get_history_id_to_assets_address_from_html(html) == {
        "333_444": ["730", "2", "456"]
    }


def test_build_buy_order_data() -> None:
    data = build_buy_order_data(
        "sess", "AK-47", "1.50", 2, GameOptions.CS, Currency.USD
    )
    assert data["sessionid"] == "sess"
    assert data["currency"] == Currency.USD.value
    assert data["appid"] == GameOptions.CS.app_id
    assert data["price_total"] == "3.00"  # 1.50 * 2
    assert data["quantity"] == 2


def test_market_listing_referer_quotes_name() -> None:
    referer = market_listing_referer(GameOptions.CS, "AK-47 | Redline")
    assert referer.endswith("/market/listings/730/AK-47%20%7C%20Redline")
