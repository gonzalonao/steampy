import json
import os
import random
import time
import urllib.parse
from decimal import Decimal
from http import HTTPStatus

from requests import Session

from steampy.confirmation import ConfirmationExecutor
from steampy.exceptions import ApiException, TooManyRequests
from steampy.models import Currency, GameOptions, SteamUrl
from steampy.utils import (
    get_listing_id_to_assets_address_from_html,
    get_history_id_to_assets_address_from_html,
    get_market_listings_from_html,
    get_market_sell_listings_from_api,
    get_history_from_api,
    login_required,
    merge_items_with_descriptions_from_listing,
    merge_items_with_descriptions_from_history,
    text_between,
)


# Steam's /market/myhistory/render endpoint accepts up to 500 rows per request;
# larger counts are silently capped/rejected. Fewer, larger pages mean fewer
# requests and therefore less rate-limit pressure, so we page at the max by
# default. Override per-call (count=) or globally via MARKET_HISTORY_PAGE_SIZE.
DEFAULT_MARKET_HISTORY_PAGE_SIZE = 500
MAX_MARKET_HISTORY_PAGE_SIZE = 500


# Rate-limit handling for market-history paging. Steam burst-throttles per IP
# and per proxy, so retrying a 429 immediately only deepens the throttle it is
# trying to escape: each one sleeps with exponential backoff + jitter before the
# next attempt (which also rotates to the next proxy). 429s draw on their own
# budget rather than the caller's max_retries, which stays reserved for genuine
# failures. Mirrors the pattern in the toolkit's steam_orderbook._backoff.
RATE_LIMIT_RETRIES = 10
RATE_LIMIT_BASE_SLEEP = 1.0   # seconds; doubled per consecutive 429
RATE_LIMIT_MAX_SLEEP = 30.0   # ceiling for both computed and Retry-After waits


def _rate_limit_sleep_seconds(resp, consecutive: int) -> float:
    """Seconds to wait after a 429, honouring ``Retry-After`` when Steam sends it.

    Falls back to exponential backoff (``base * 2 ** consecutive``) plus jitter,
    so concurrent account fetches don't resynchronise onto the same retry beat.
    Both paths are capped at ``RATE_LIMIT_MAX_SLEEP`` so a stray or hostile
    header can't stall a run indefinitely.
    """
    retry_after = resp.headers.get('Retry-After') if resp is not None else None
    if retry_after:
        try:
            return min(float(retry_after), RATE_LIMIT_MAX_SLEEP)
        except ValueError:
            pass  # Retry-After may be an HTTP-date; fall through to backoff
    backoff = RATE_LIMIT_BASE_SLEEP * (2 ** consecutive)
    return min(backoff, RATE_LIMIT_MAX_SLEEP) + random.random()


def _resolve_history_page_size(count: int | None) -> int:
    """Resolve the market-history page size.

    Precedence: explicit ``count`` arg > ``MARKET_HISTORY_PAGE_SIZE`` env var >
    ``DEFAULT_MARKET_HISTORY_PAGE_SIZE``. The result is clamped to
    ``[1, MAX_MARKET_HISTORY_PAGE_SIZE]`` since Steam rejects larger counts.
    """
    if count is None:
        raw = os.getenv('MARKET_HISTORY_PAGE_SIZE')
        if raw and raw.strip():
            try:
                count = int(raw)
            except ValueError:
                print(
                    f'[WARNING] Invalid MARKET_HISTORY_PAGE_SIZE={raw!r}; '
                    f'falling back to default {DEFAULT_MARKET_HISTORY_PAGE_SIZE}.'
                )
                count = DEFAULT_MARKET_HISTORY_PAGE_SIZE
        else:
            count = DEFAULT_MARKET_HISTORY_PAGE_SIZE
    return max(1, min(count, MAX_MARKET_HISTORY_PAGE_SIZE))


class SteamMarket:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._steam_guard = None
        self._session_id = None
        self.was_login_executed = False

    def _set_login_executed(self, steamguard: dict, session_id: str) -> None:
        self._steam_guard = steamguard
        self._session_id = session_id
        self.was_login_executed = True

    def fetch_price(
        self, item_hash_name: str, game: GameOptions, currency: Currency = Currency.USD, country='PL',
    ) -> dict:
        url = f'{SteamUrl.COMMUNITY_URL}/market/priceoverview/'
        params = {
            'country': country,
            'currency': currency.value,
            'appid': game.app_id,
            'market_hash_name': item_hash_name,
        }

        response = self._session.rotating_get(url, params=params)
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise TooManyRequests('You can fetch maximum 20 prices in 60s period')

        return response.json()

    @login_required
    def fetch_price_history(self, item_hash_name: str, game: GameOptions) -> dict:
        url = f'{SteamUrl.COMMUNITY_URL}/market/pricehistory/'
        params = {'country': 'PL', 'appid': game.app_id, 'market_hash_name': item_hash_name}

        response = self._session.rotating_get(url, params=params)
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise TooManyRequests('You can fetch maximum 20 prices in 60s period')

        return response.json()

    @login_required
    def create_sell_order(self, assetid: str, game: GameOptions, money_to_receive: str, amount: int = 1) -> dict:
        data = {
            'assetid': assetid,
            'sessionid': self._session_id,
            'contextid': game.context_id,
            'appid': game.app_id,
            'amount': amount,
            'price': money_to_receive,
        }
        headers = {'Referer': f'{SteamUrl.COMMUNITY_URL}/profiles/{self._steam_guard["steamid"]}/inventory'}

        response = self._session.rotating_post(f'{SteamUrl.COMMUNITY_URL}/market/sellitem/', data, headers=headers).json()
        has_pending_confirmation = 'pending confirmation' in response.get('message', '')
        if response.get('needs_mobile_confirmation') or (not response.get('success') and has_pending_confirmation):
            if 'identity_secret' in self._steam_guard:
                return self._confirm_sell_listing(assetid)
            # No identity_secret available (cookie-auth) — return as-is;
            # the listing is created but needs manual confirmation in the Steam app.
            return response

        return response

    @login_required
    def create_buy_order(
        self,
        market_name: str,
        price_single_item: str,
        quantity: int,
        game: GameOptions,
        currency: Currency = Currency.EURO,
        proxy: dict = None,
    ) -> dict:
        data = {
            'sessionid': self._session_id,
            'currency': currency.value,
            'appid': game.app_id,
            'market_hash_name': market_name,
            'price_total': str(Decimal(price_single_item) * Decimal(quantity)),
            'quantity': quantity,
            'confirmation': '1'
        }
        headers = {
            'Referer': f'{SteamUrl.COMMUNITY_URL}/market/listings/{game.app_id}/{urllib.parse.quote(market_name)}',
        }

        response = self._session.post(f'{SteamUrl.COMMUNITY_URL}/market/createbuyorder/', data, headers=headers, proxies=proxy)

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
        data = {
            'sessionid': self._session_id,
            'currency': currency.value,
            'subtotal': price - fee,
            'fee': fee,
            'total': price,
            'quantity': '1',
        }
        headers = {
            'Referer': f'{SteamUrl.COMMUNITY_URL}/market/listings/{game.app_id}/{urllib.parse.quote(market_name)}',
        }
        response = self._session.post(
            f'{SteamUrl.COMMUNITY_URL}/market/buylisting/{market_id}', data, headers=headers,
        ).json()

        try:
            if (success := response['wallet_info']['success']) != 1:
                raise ApiException(
                    f'There was a problem buying this item. Are you using the right currency? success: {success}',
                )
        except Exception:
            raise ApiException(f'There was a problem buying this item. Message: {response.get("message")}')

        return response

    @login_required
    def cancel_sell_order(self, sell_listing_id: str) -> None:
        data = {'sessionid': self._session_id}
        headers = {'Referer': f'{SteamUrl.COMMUNITY_URL}/market/'}
        url = f'{SteamUrl.COMMUNITY_URL}/market/removelisting/{sell_listing_id}'

        response = self._session.post(url, data=data, headers=headers)
        if response.status_code != HTTPStatus.OK:
            raise ApiException(f'There was a problem removing the listing. HTTP code: {response.status_code}')

    @login_required
    def cancel_buy_order(self, buy_order_id) -> dict:
        data = {'sessionid': self._session_id, 'buy_orderid': buy_order_id}
        headers = {'Referer': f'{SteamUrl.COMMUNITY_URL}/market'}
        response = self._session.post(f'{SteamUrl.COMMUNITY_URL}/market/cancelbuyorder/', data, headers=headers).json()

        if (success := response.get('success')) != 1:
            raise ApiException(f'There was a problem canceling the order. success: {success}')

        return response

    def _confirm_sell_listing(self, asset_id: str) -> dict:
        con_executor = ConfirmationExecutor(
            self._steam_guard['identity_secret'], self._steam_guard['steamid'], self._session,
        )
        return con_executor.confirm_sell_listing(asset_id)
    
    @login_required
    def get_my_market_listings(self) -> dict:
        response = self._session.rotating_get(f'{SteamUrl.COMMUNITY_URL}/market')
        if response.status_code != HTTPStatus.OK:
            raise ApiException(f'There was a problem getting the listings. HTTP code: {response.status_code}')

        assets_descriptions = json.loads(text_between(response.text, 'var g_rgAssets = ', ';\n'))
        listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(response.text)
        listings = get_market_listings_from_html(response.text)
        listings = merge_items_with_descriptions_from_listing(
            listings, listing_id_to_assets_address, assets_descriptions,
        )

        if '<span id="tabContentsMyActiveMarketListings_end">' in response.text:
            n_showing = int(text_between(response.text, '<span id="tabContentsMyActiveMarketListings_end">', '</span>'))
            n_total = int(
                text_between(response.text, '<span id="tabContentsMyActiveMarketListings_total">', '</span>').replace(
                    ',', '',
                ),
            )

            if n_showing < n_total < 1000:
                url = f'{SteamUrl.COMMUNITY_URL}/market/mylistings/render/?query=&start={n_showing}&count={-1}'
                response = self._session.rotating_get(url)
                if response.status_code != HTTPStatus.OK:
                    raise ApiException(f'There was a problem getting the listings. HTTP code: {response.status_code}')

                jresp = response.json()
                listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(jresp.get('hovers'))
                listings_2 = get_market_sell_listings_from_api(jresp.get('results_html'))
                listings_2 = merge_items_with_descriptions_from_listing(
                    listings_2, listing_id_to_assets_address, jresp.get('assets'),
                )
                listings['sell_listings'] = {**listings['sell_listings'], **listings_2['sell_listings']}
            else:
                for i in range(0, n_total, 100):
                    url = f'{SteamUrl.COMMUNITY_URL}/market/mylistings/?query=&start={n_showing + i}&count={100}'
                    response = self._session.rotating_get(url)
                    if response.status_code != HTTPStatus.OK:
                        raise ApiException(
                            f'There was a problem getting the listings. HTTP code: {response.status_code}',
                        )
                    jresp = response.json()
                    listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(jresp.get('hovers'))
                    listings_2 = get_market_sell_listings_from_api(jresp.get('results_html'))
                    listings_2 = merge_items_with_descriptions_from_listing(
                        listings_2, listing_id_to_assets_address, jresp.get('assets'),
                    )
                    listings['sell_listings'] = {**listings['sell_listings'], **listings_2['sell_listings']}

        return listings

    @login_required
    def get_market_history(self, max_retries: int = 5, count: int | None = None):
        url = f'{SteamUrl.COMMUNITY_URL}/market/myhistory/render/'
        page_size = _resolve_history_page_size(count)

        # helper to perform a GET with retry loop
        def _query(params: dict) -> 'requests.Response':
            last_resp = None
            rate_limited = 0
            attempt = 0
            while attempt < max_retries:
                resp = self._session.rotating_get(url, params=params)
                if resp.status_code == HTTPStatus.OK:
                    return resp
                last_resp = resp
                # Throttling is transient, not a failure: sleep it off (the next
                # attempt rotates proxies too) without spending the retry budget.
                if (resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
                        and rate_limited < RATE_LIMIT_RETRIES):
                    delay = _rate_limit_sleep_seconds(resp, rate_limited)
                    rate_limited += 1
                    print(
                        f'[DEBUG] Rate limited (429) {rate_limited}/{RATE_LIMIT_RETRIES}; '
                        f'sleeping {delay:.1f}s before retrying...'
                    )
                    time.sleep(delay)
                    continue
                attempt += 1
                print(
                    f'[DEBUG] Attempt {attempt}/{max_retries} failed with HTTP code '
                    f'{resp.status_code}. Retrying...'
                )
            # failed all attempts
            raise ApiException(
                f'There was a problem getting the market history after {max_retries} attempts '
                f'({rate_limited} rate-limit retries). Last HTTP code: {last_resp.status_code}'
            )

        # initial request to learn total_count
        params = {'start': 0, 'count': 1}
        response = _query(params)

        all_history = {}
        jresp = response.json()
        total_count = jresp.get('total_count', 0)

        # Step by page_size across [0, total_count); the final page's count only
        # over-reaches into an empty range, so no trailing empty request is made.
        for i in range(0, total_count, page_size):
            params = {'start': i, 'count': page_size}
            response = _query(params)
            jresp = response.json()
            history_row_to_assets_address = get_history_id_to_assets_address_from_html(jresp.get('hovers'))
            history = get_history_from_api(jresp.get('results_html'))
            history = merge_items_with_descriptions_from_history(
                        history, history_row_to_assets_address, jresp.get('assets')
                        )
            all_history = {**all_history, **history}

        return all_history