from __future__ import annotations

import json
import re
import urllib.parse as urlparse
from decimal import Decimal

import requests
import time

from steampy import guard
from steampy.confirmation import ConfirmationExecutor
from steampy.exceptions import ApiException, SevenDaysHoldException, TooManyRequests
from steampy.login import InvalidCredentials, LoginExecutor
from steampy.market import SteamMarket
from steampy.models import Asset, GameOptions, SteamUrl, TradeOfferState
from steampy.rot_proxy import RotatingProxySession
from steampy.utils import (
    account_id_to_steam_id,
    get_description_key,
    get_key_value_from_url,
    login_required,
    merge_items,
    merge_items_with_descriptions_from_inventory,
    merge_items_with_descriptions_from_offer,
    merge_items_with_descriptions_from_offers,
    steam_id_to_account_id,
    text_between,
    texts_between,
)


class SteamClient:
    def __init__(
        self,
        login_cookies: dict,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        steam_guard: str | None = None,
        proxies: dict | None = None,
        account_name: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._session = RotatingProxySession()
        self._sessionid = login_cookies.get('sessionid')
        self.account_name = account_name

        if proxies:
            print(f"[DEBUG] Setting up proxies for account {account_name or 'unknown'}...")
            self._session.set_proxies_list(proxies, skip_ping=True)

        self.steam_guard_string = steam_guard
        if self.steam_guard_string is not None:
            self.steam_guard = guard.load_steam_guard(self.steam_guard_string)
        else:
            self.steam_guard = None

        self.was_login_executed = False
        self.username = username
        self._password = password
        self.market = SteamMarket(self._session)
        self._access_token = None

        self.set_login_cookies(login_cookies)

    def set_login_cookies(self, cookies: dict) -> None:
        self._session.cookies.update(cookies)
        self.was_login_executed = True
        if self.steam_guard is None:
            self.steam_guard = {'steamid': str(self.get_steam_id())}
        self.market._set_login_executed(self.steam_guard, self._get_session_id())

    @login_required
    def get_steam_id(self) -> int:
        url = SteamUrl.COMMUNITY_URL
        response = self._session.get(url)
        if steam_id := re.search(r'g_steamID = "(\d+)";', response.text):
            return int(steam_id.group(1))
        account_info = f" for account '{self.account_name}'" if self.account_name else ""
        raise ValueError(f'Invalid steam_id{account_info}: {steam_id}')

    def _set_access_token(self) ->str :
        steam_login_secure_cookies = [cookie for cookie in self._session.cookies if cookie.name == 'steamLoginSecure']
        cookie_value = steam_login_secure_cookies[0].value
        decoded_cookie_value = urlparse.unquote(cookie_value)
        access_token_parts = decoded_cookie_value.split('||')
        if len(access_token_parts) < 2:
            print(decoded_cookie_value)
            raise ValueError('Access token not found in steamLoginSecure cookie')
        access_token = access_token_parts[1]
        return access_token

    def __enter__(self):
        self.login(self.username, self._password, self.steam_guard_string)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logout()

    def api_call(
        self, method: str, interface: str, api_method: str, version: str, params: dict | None = None,
    ) -> requests.Response:
        url = f'{SteamUrl.API_URL}/{interface}/{api_method}/{version}'
        response = self._session.get(url, params=params) if method == 'GET' else self._session.post(url, data=params)

        if self.is_invalid_api_key(response):
            raise InvalidCredentials('Invalid API key')

        return response

    @staticmethod
    def is_invalid_api_key(response: requests.Response) -> bool:
        msg = 'Access is denied. Retrying will not help. Please verify your <pre>key=</pre> parameter'
        return msg in response.text

    @login_required
    def get_my_inventory(self, game: GameOptions, merge: bool = True, count: int = 5000) -> dict:
        steam_id = self.steam_guard['steamid']
        return self.get_partner_inventory(steam_id, game, merge, count)

    @login_required
    def get_partner_inventory(
        self, partner_steam_id: str, game: GameOptions, merge: bool = True, count: int = 5000,
    ) -> dict:
        url = f'{SteamUrl.COMMUNITY_URL}/inventory/{partner_steam_id}/{game.app_id}/{game.context_id}'
        params: dict = {'l': 'english', 'count': 2000}

        all_assets: dict = {}        # assetid -> asset dict
        all_descriptions: dict = {}  # classid_instanceid -> description dict

        while True:
            full_response = self._session.rotating_get(url, params=params)

            if full_response.status_code == 429:
                raise TooManyRequests('Too many requests, try again later.')

            response_dict = full_response.json()
            if response_dict is None:
                raise ApiException('Response is None.')
            if response_dict.get('success') != 1:
                raise ApiException(f"Success value should be 1. Actual value: {response_dict.get('success')}")

            for asset in response_dict.get('assets', []):
                all_assets[asset['assetid']] = asset
            for desc in response_dict.get('descriptions', []):
                all_descriptions[get_description_key(desc)] = desc

            last_assetid = response_dict.get('last_assetid')
            if not last_assetid:
                break
            params['start_assetid'] = last_assetid
            time.sleep(1)  # avoid rate limiting between pages

        if not merge:
            return {
                'assets': list(all_assets.values()),
                'descriptions': list(all_descriptions.values()),
            }

        if not all_assets:
            return {}

        return merge_items(all_assets, all_descriptions, context_id=game.context_id)

    def _get_session_id(self) -> str:
        return self._sessionid

    def get_trade_offers_summary(self) -> dict:
        params = {'key': self._api_key}
        return self.api_call('GET', 'IEconService', 'GetTradeOffersSummary', 'v1', params).json()

    def get_trade_offers(self, merge: bool = True, get_sent_offers: bool = True, get_received_offers: bool = True, use_webtoken: bool =False, max_retry:int = 5) -> dict:
        params = {'key' if not use_webtoken else 'access_token': self._api_key if not use_webtoken else self._access_token,
                  'get_sent_offers': int(get_sent_offers),
                  'get_received_offers': int(get_received_offers),
                  'get_descriptions': 1,
                  'language': 'english',
                  'active_only': 1,
                  'historical_only': 0,
                  'time_historical_cutoff': ''}

        response = self._try_to_get_trade_offers(params ,max_retry)
        if response is None:
            raise ApiException('Cannot get proper json from get_trade_offers method')
        response_with_active_offers = self._filter_non_active_offers(response)
        if merge:
            return merge_items_with_descriptions_from_offers(response_with_active_offers)
        else:
            return response_with_active_offers

    def _try_to_get_trade_offers(self, params:dict, max_retry: int) -> dict | None:
        response = None
        for _ in range(max_retry):
            try:
                response = self.api_call('GET', 'IEconService', 'GetTradeOffers', 'v1', params).json()
                break
            except json.decoder.JSONDecodeError:
                time.sleep(2)
                continue
        return response

    @staticmethod
    def _filter_non_active_offers(offers_response):
        offers_received = offers_response['response'].get('trade_offers_received', [])
        offers_sent = offers_response['response'].get('trade_offers_sent', [])

        offers_response['response']['trade_offers_received'] = list(
            filter(lambda offer: offer['trade_offer_state'] == TradeOfferState.Active, offers_received),
        )
        offers_response['response']['trade_offers_sent'] = list(
            filter(lambda offer: offer['trade_offer_state'] == TradeOfferState.Active, offers_sent),
        )

        return offers_response

    def get_trade_offer(self, trade_offer_id: str, merge: bool = True, use_webtoken:bool =False) -> dict:
        params = {
            'tradeofferid': trade_offer_id,
            'language': 'english'}
        if use_webtoken:
            params['access_token'] = self._access_token
        else:
            params['key'] = self._api_key

        response = self.api_call('GET', 'IEconService', 'GetTradeOffer', 'v1', params).json()

        if merge and 'descriptions' in response['response']:
            descriptions = {get_description_key(offer): offer for offer in response['response']['descriptions']}
            offer = response['response']['offer']
            response['response']['offer'] = merge_items_with_descriptions_from_offer(offer, descriptions)

        return response

    def get_trade_history(
        self,
        max_trades: int = 100,
        start_after_time=None,
        start_after_tradeid=None,
        get_descriptions: bool = True,
        navigating_back: bool = True,
        include_failed: bool = True,
        include_total: bool = True,
    ) -> dict:
        params = {
            'key': self._api_key,
            'max_trades': max_trades,
            'start_after_time': start_after_time,
            'start_after_tradeid': start_after_tradeid,
            'get_descriptions': get_descriptions,
            'navigating_back': navigating_back,
            'include_failed': include_failed,
            'include_total': include_total,
        }
        return self.api_call('GET', 'IEconService', 'GetTradeHistory', 'v1', params).json()

    @login_required
    def get_trade_receipt(self, trade_id: str):
        html = self._session.get(f'https://steamcommunity.com/trade/{trade_id}/receipt').content.decode()
        return [json.loads(item) for item in texts_between(html, 'oItem = ', ';\r\n\toItem')]

    @login_required
    def accept_trade_offer(self, trade_offer_id: str) -> dict:
        trade = self.get_trade_offer(trade_offer_id, use_webtoken=True)
        trade_offer_state = TradeOfferState(trade['response']['offer']['trade_offer_state'])
        if trade_offer_state is not TradeOfferState.Active:
            raise ApiException(f'Invalid trade offer state: {trade_offer_state.name} ({trade_offer_state.value})')

        partner = self._fetch_trade_partner_id(trade_offer_id)
        session_id = self._get_session_id()
        accept_url = f'{SteamUrl.COMMUNITY_URL}/tradeoffer/{trade_offer_id}/accept'
        params = {
            'sessionid': session_id,
            'tradeofferid': trade_offer_id,
            'serverid': '1',
            'partner': partner,
            'captcha': '',
        }
        headers = {'Referer': self._get_trade_offer_url(trade_offer_id)}

        response = self._session.post(accept_url, data=params, headers=headers).json()
        if response.get('needs_mobile_confirmation', False):
            return self._confirm_transaction(trade_offer_id)

        return response

    def _fetch_trade_partner_id(self, trade_offer_id: str) -> str:
        url = self._get_trade_offer_url(trade_offer_id)
        offer_response_text = self._session.get(url).text

        if 'You have logged in from a new device. In order to protect the items' in offer_response_text:
            raise SevenDaysHoldException("Account has logged in a new device and can't trade for 7 days")

        return text_between(offer_response_text, "var g_ulTradePartnerSteamID = '", "';")

    def _confirm_transaction(self, trade_offer_id: str) -> dict:
        confirmation_executor = ConfirmationExecutor(
            self.steam_guard['identity_secret'], self.steam_guard['steamid'], self._session,
        )
        return confirmation_executor.send_trade_allow_request(trade_offer_id)

    def decline_trade_offer(self, trade_offer_id: str) -> dict:
        url = f'https://steamcommunity.com/tradeoffer/{trade_offer_id}/decline'
        return self._session.post(url, data={'sessionid': self._get_session_id()}).json()

    def cancel_trade_offer(self, trade_offer_id: str) -> dict:
        url = f'https://steamcommunity.com/tradeoffer/{trade_offer_id}/cancel'
        return self._session.post(url, data={'sessionid': self._get_session_id()}).json()

    @login_required
    def make_offer(
        self, items_from_me: list[Asset], items_from_them: list[Asset], partner_steam_id: str, message: str = '',
    ) -> dict:
        offer = self._create_offer_dict(items_from_me, items_from_them)
        session_id = self._get_session_id()
        url = f'{SteamUrl.COMMUNITY_URL}/tradeoffer/new/send'
        server_id = 1
        params = {
            'sessionid': session_id,
            'serverid': server_id,
            'partner': partner_steam_id,
            'tradeoffermessage': message,
            'json_tradeoffer': json.dumps(offer),
            'captcha': '',
            'trade_offer_create_params': '{}',
        }
        partner_account_id = steam_id_to_account_id(partner_steam_id)
        headers = {
            'Referer': f'{SteamUrl.COMMUNITY_URL}/tradeoffer/new/?partner={partner_account_id}',
            'Origin': SteamUrl.COMMUNITY_URL,
        }

        response = self._session.post(url, data=params, headers=headers).json()
        if response.get('needs_mobile_confirmation'):
            response.update(self._confirm_transaction(response['tradeofferid']))

        return response

    @staticmethod
    def _create_offer_dict(items_from_me: list[Asset], items_from_them: list[Asset]) -> dict:
        return {
            'newversion': True,
            'version': 4,
            'me': {'assets': [asset.to_dict() for asset in items_from_me], 'currency': [], 'ready': False},
            'them': {'assets': [asset.to_dict() for asset in items_from_them], 'currency': [], 'ready': False},
        }

    @login_required
    def make_offer_with_url(
        self,
        items_from_me: list[Asset],
        items_from_them: list[Asset],
        trade_offer_url: str,
        message: str = '',
        case_sensitive: bool = True,
        confirm_trade: bool = True,
    ) -> dict:
        token = get_key_value_from_url(trade_offer_url, 'token', case_sensitive)
        partner_account_id = get_key_value_from_url(trade_offer_url, 'partner', case_sensitive)
        partner_steam_id = account_id_to_steam_id(partner_account_id)
        offer = self._create_offer_dict(items_from_me, items_from_them)
        session_id = self._get_session_id()
        url = f'{SteamUrl.COMMUNITY_URL}/tradeoffer/new/send'
        server_id = 1
        trade_offer_create_params = {'trade_offer_access_token': token}
        params = {
            'sessionid': session_id,
            'serverid': server_id,
            'partner': partner_steam_id,
            'tradeoffermessage': message,
            'json_tradeoffer': json.dumps(offer),
            'captcha': '',
            'trade_offer_create_params': json.dumps(trade_offer_create_params),
        }

        headers = {
            'Referer': f'{SteamUrl.COMMUNITY_URL}{urlparse.urlparse(trade_offer_url).path}',
            'Origin': SteamUrl.COMMUNITY_URL,
        }

        response = self._session.post(url, data=params, headers=headers).json()
        if confirm_trade and response.get('needs_mobile_confirmation'):
            response.update(self._confirm_transaction(response['tradeofferid']))

        return response

    @staticmethod
    def _get_trade_offer_url(trade_offer_id: str) -> str:
        return f'{SteamUrl.COMMUNITY_URL}/tradeoffer/{trade_offer_id}'

    @login_required
    # If convert_to_decimal = False, the price will be returned WITHOUT a decimal point.
    def get_wallet_balance(self, convert_to_decimal: bool = True, on_hold: bool = False) -> str | Decimal:
        response = self._session.get(f'{SteamUrl.COMMUNITY_URL}/market')
        wallet_info_match = re.search(r'var g_rgWalletInfo = (.*?);', response.text)
        if wallet_info_match:
            balance_dict_str = wallet_info_match.group(1)
            balance_dict = json.loads(balance_dict_str)
        else:
            raise Exception('Unable to get wallet balance string match')
        balance_dict_key = 'wallet_delayed_balance' if on_hold else 'wallet_balance'
        if convert_to_decimal:
            return Decimal(balance_dict[balance_dict_key]) / 100
        return balance_dict[balance_dict_key]
