"""High-level client for Steam trading, inventory and market access."""

from __future__ import annotations

import json
import re
import urllib.parse as urlparse
from decimal import Decimal
from http import HTTPStatus

import requests

from steampy import guard
from steampy.confirmation import ConfirmationExecutor
from steampy.exceptions import (
    ApiException,
    InvalidCredentials,
    ProxyConnectionError,
    SevenDaysHoldException,
    TooManyRequests,
)
from steampy.login import LoginExecutor
from steampy.market import SteamMarket
from steampy.models import Asset, GameOptions, SteamUrl, TradeOfferState
from steampy.session import RotatingProxySession
from steampy.utils import (
    account_id_to_steam_id,
    get_description_key,
    get_key_value_from_url,
    login_required,
    merge_items_with_descriptions_from_inventory,
    merge_items_with_descriptions_from_offer,
    merge_items_with_descriptions_from_offers,
    ping_proxy,
    retry_call,
    steam_id_to_account_id,
    text_between,
    texts_between,
)

# Steam caps a single inventory request at 2000 items.
_MAX_INVENTORY_COUNT = 2000
# Steam's trade-offer create requests always target server id 1.
_TRADE_SERVER_ID = 1
# Default attempts when GetTradeOffers returns malformed JSON.
_TRADE_OFFERS_MAX_RETRY = 5
_TRADE_OFFERS_RETRY_DELAY = 2.0


class SteamClient:
    """Authenticated entry point for Steam trade, inventory and market actions."""

    def __init__(
        self,
        api_key: str,
        username: str | None = None,
        password: str | None = None,
        steam_guard: str | None = None,
        login_cookies: dict | None = None,
        proxies: dict | list[dict] | None = None,
    ) -> None:
        self._api_key = api_key
        self._session = RotatingProxySession()

        if proxies:
            self.set_proxies(proxies)

        self.steam_guard_string = steam_guard
        self.steam_guard: dict | None = (
            guard.load_steam_guard(steam_guard) if steam_guard is not None else None
        )

        self.was_login_executed = False
        self.username = username
        self._password = password
        self.market = SteamMarket(self._session)
        self._access_token: str | None = None

        if login_cookies:
            self.set_login_cookies(login_cookies)

    def set_proxies(self, proxies: dict | list[dict]) -> None:
        """Configure one proxy (dict) or a rotating pool (list of dicts).

        Args:
            proxies: A single ``requests``-style proxy dict, or a list of them
                to round-robin over.

        Raises:
            TypeError: If ``proxies`` is neither a dict nor a list.
            ProxyConnectionError: If a single proxy cannot reach Steam.
        """
        if isinstance(proxies, list):
            self._session.set_proxies_list(proxies)
            return
        if not isinstance(proxies, dict):
            raise TypeError(
                "Proxy must be a dict or a list of dicts. Example: "
                '{"http": "http://login:password@host:port", "https": "http://login:password@host:port"}',
            )
        if not ping_proxy(proxies):
            raise ProxyConnectionError("Proxy not working for steamcommunity.com")
        self._session.proxies.update(proxies)
        self._session.set_proxies_list([proxies], skip_ping=True)

    def set_login_cookies(self, cookies: dict) -> None:
        """Authenticate using existing session cookies instead of credentials.

        The cookies are scoped to the community and store domains so that both
        outgoing requests and the domain-filtered session-id lookup resolve them.
        """
        for name, value in cookies.items():
            for domain in ("steamcommunity.com", "store.steampowered.com"):
                self._session.cookies.set(name, value, domain=domain, path="/")
        self.was_login_executed = True
        if self.steam_guard is None:
            self.steam_guard = {"steamid": str(self.get_steam_id())}
        self.market._set_login_executed(self.steam_guard, self._get_session_id())

    @login_required
    def get_steam_id(self) -> int:
        """Return the logged-in account's 64-bit steam id."""
        response = self._session.get(SteamUrl.COMMUNITY_URL)
        if steam_id := re.search(r'g_steamID = "(\d+)";', response.text):
            return int(steam_id.group(1))
        raise ValueError("Could not determine the steam id from the community page")

    def login(
        self,
        username: str | None = None,
        password: str | None = None,
        steam_guard: str | None = None,
    ) -> None:
        """Log in with credentials, reusing the live session when possible."""
        invalid_client_credentials_is_present = None in {
            self.username,
            self._password,
            self.steam_guard_string,
        }
        invalid_login_credentials_is_present = None in {username, password, steam_guard}

        if (
            invalid_client_credentials_is_present
            and invalid_login_credentials_is_present
        ):
            raise InvalidCredentials(
                "You have to pass username, password and steam_guard parameters "
                'when using "login" method',
            )

        if invalid_client_credentials_is_present:
            # Reaching here means the login parameters are all present.
            assert steam_guard is not None
            self.steam_guard_string = steam_guard
            self.steam_guard = guard.load_steam_guard(steam_guard)
            self.username = username
            self._password = password

        if self.was_login_executed and self.is_session_alive():
            return  # Session is alive, no need to login again

        # Credentials are guaranteed present here: the checks above raise
        # otherwise.
        assert self.username is not None
        assert self._password is not None
        assert self.steam_guard is not None

        self._session.cookies.set("steamRememberLogin", "true")
        LoginExecutor(
            self.username,
            self._password,
            self.steam_guard["shared_secret"],
            self._session,
        ).login()
        self.was_login_executed = True
        self.market._set_login_executed(self.steam_guard, self._get_session_id())
        self._access_token = self._set_access_token()

    def _set_access_token(self) -> str:
        steam_login_secure_cookies = [
            cookie
            for cookie in self._session.cookies
            if cookie.name == "steamLoginSecure"
        ]
        cookie_value = steam_login_secure_cookies[0].value
        if cookie_value is None:
            raise ValueError("steamLoginSecure cookie has no value")
        decoded_cookie_value = urlparse.unquote(cookie_value)
        access_token_parts = decoded_cookie_value.split("||")
        if len(access_token_parts) < 2:
            raise ValueError("Access token not found in steamLoginSecure cookie")
        return access_token_parts[1]

    @login_required
    def logout(self) -> None:
        """Log out and invalidate the current session."""
        url = f"{SteamUrl.COMMUNITY_URL}/login/logout/"
        data = {"sessionid": self._get_session_id()}
        self._session.post(url, data=data)
        if self.is_session_alive():
            raise ApiException("Logout unsuccessful")
        self.was_login_executed = False

    def __enter__(self) -> SteamClient:
        self.login(self.username, self._password, self.steam_guard_string)
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.logout()

    @login_required
    def is_session_alive(self) -> bool:
        """Return whether the community session is still authenticated."""
        steam_login = self.username or ""
        main_page_response = self._session.get(SteamUrl.COMMUNITY_URL)
        return steam_login.lower() in main_page_response.text.lower()

    def api_call(
        self,
        method: str,
        interface: str,
        api_method: str,
        version: str,
        params: dict | None = None,
    ) -> requests.Response:
        """Call a Steam Web API endpoint and return the raw response."""
        url = f"{SteamUrl.API_URL}/{interface}/{api_method}/{version}"
        response = (
            self._session.get(url, params=params)
            if method == "GET"
            else self._session.post(url, data=params)
        )
        if self.is_invalid_api_key(response):
            raise InvalidCredentials("Invalid API key")
        return response

    @staticmethod
    def is_invalid_api_key(response: requests.Response) -> bool:
        """Return whether the response indicates a rejected API key."""
        msg = (
            "Access is denied. Retrying will not help. "
            "Please verify your <pre>key=</pre> parameter"
        )
        return msg in response.text

    @login_required
    def get_my_inventory(
        self, game: GameOptions, merge: bool = True, count: int = _MAX_INVENTORY_COUNT
    ) -> dict:
        """Return the logged-in account's inventory for ``game``."""
        assert self.steam_guard is not None
        steam_id = self.steam_guard["steamid"]
        return self.get_partner_inventory(steam_id, game, merge, count)

    @login_required
    def get_partner_inventory(
        self,
        partner_steam_id: str,
        game: GameOptions,
        merge: bool = True,
        count: int = _MAX_INVENTORY_COUNT,
    ) -> dict:
        """Return another account's inventory for ``game``.

        ``count`` is clamped to Steam's per-request maximum of 2000 items.
        """
        url = (
            f"{SteamUrl.COMMUNITY_URL}/inventory/"
            f"{partner_steam_id}/{game.app_id}/{game.context_id}"
        )
        params: dict = {"l": "english", "count": min(count, _MAX_INVENTORY_COUNT)}

        full_response = self._session.get(url, params=params)
        if full_response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise TooManyRequests("Too many requests, try again later.")

        response_dict = full_response.json()
        if response_dict is None or response_dict.get("success") != 1:
            raise ApiException("Success value should be 1.")

        return (
            merge_items_with_descriptions_from_inventory(response_dict, game)
            if merge
            else response_dict
        )

    def _get_session_id(self) -> str:
        cookies = self._session.cookies.get_dict(domain="steamcommunity.com", path="/")
        return cookies.get("sessionid", "")

    def get_trade_offers_summary(self) -> dict:
        """Return the account's pending trade-offer counters."""
        params = {"key": self._api_key}
        return self.api_call(
            "GET", "IEconService", "GetTradeOffersSummary", "v1", params
        ).json()

    def get_trade_offers(
        self,
        merge: bool = True,
        get_sent_offers: bool = True,
        get_received_offers: bool = True,
        use_webtoken: bool = False,
        max_retry: int = _TRADE_OFFERS_MAX_RETRY,
    ) -> dict:
        """Return the account's active trade offers, retrying malformed JSON."""
        credential = (
            {"access_token": self._access_token}
            if use_webtoken
            else {"key": self._api_key}
        )
        params = {
            **credential,
            "get_sent_offers": int(get_sent_offers),
            "get_received_offers": int(get_received_offers),
            "get_descriptions": 1,
            "language": "english",
            "active_only": 1,
            "historical_only": 0,
            "time_historical_cutoff": "",
        }

        response = self._try_to_get_trade_offers(params, max_retry)
        response = self._filter_non_active_offers(response)
        return (
            merge_items_with_descriptions_from_offers(response) if merge else response
        )

    def _try_to_get_trade_offers(self, params: dict, max_retry: int) -> dict:
        try:
            return retry_call(
                lambda: self.api_call(
                    "GET", "IEconService", "GetTradeOffers", "v1", params
                ).json(),
                attempts=max_retry,
                retry_exceptions=(json.decoder.JSONDecodeError,),
                delay=_TRADE_OFFERS_RETRY_DELAY,
            )
        except json.decoder.JSONDecodeError as exc:
            raise ApiException(
                "Cannot get proper json from get_trade_offers method"
            ) from exc

    @staticmethod
    def _filter_non_active_offers(offers_response: dict) -> dict:
        offers_received = offers_response["response"].get("trade_offers_received", [])
        offers_sent = offers_response["response"].get("trade_offers_sent", [])
        offers_response["response"]["trade_offers_received"] = [
            offer
            for offer in offers_received
            if offer["trade_offer_state"] == TradeOfferState.Active
        ]
        offers_response["response"]["trade_offers_sent"] = [
            offer
            for offer in offers_sent
            if offer["trade_offer_state"] == TradeOfferState.Active
        ]
        return offers_response

    def get_trade_offer(
        self, trade_offer_id: str, merge: bool = True, use_webtoken: bool = False
    ) -> dict:
        """Return a single trade offer, optionally merging item descriptions."""
        params: dict = {"tradeofferid": trade_offer_id, "language": "english"}
        params["access_token" if use_webtoken else "key"] = (
            self._access_token if use_webtoken else self._api_key
        )
        response = self.api_call(
            "GET", "IEconService", "GetTradeOffer", "v1", params
        ).json()

        if merge and "descriptions" in response["response"]:
            descriptions = {
                get_description_key(offer): offer
                for offer in response["response"]["descriptions"]
            }
            offer = response["response"]["offer"]
            response["response"]["offer"] = merge_items_with_descriptions_from_offer(
                offer, descriptions
            )
        return response

    def get_trade_history(
        self,
        max_trades: int = 100,
        start_after_time: int | None = None,
        start_after_tradeid: str | None = None,
        get_descriptions: bool = True,
        navigating_back: bool = True,
        include_failed: bool = True,
        include_total: bool = True,
    ) -> dict:
        """Return the account's completed trade history."""
        params = {
            "key": self._api_key,
            "max_trades": max_trades,
            "start_after_time": start_after_time,
            "start_after_tradeid": start_after_tradeid,
            "get_descriptions": get_descriptions,
            "navigating_back": navigating_back,
            "include_failed": include_failed,
            "include_total": include_total,
        }
        return self.api_call(
            "GET", "IEconService", "GetTradeHistory", "v1", params
        ).json()

    @login_required
    def get_trade_receipt(self, trade_id: str) -> list[dict]:
        """Return the parsed items contained in a completed trade receipt."""
        html = self._session.get(
            f"{SteamUrl.COMMUNITY_URL}/trade/{trade_id}/receipt"
        ).content.decode()
        return [
            json.loads(item) for item in texts_between(html, "oItem = ", ";\r\n\toItem")
        ]

    @login_required
    def accept_trade_offer(self, trade_offer_id: str) -> dict:
        """Accept an incoming trade offer, confirming it when required."""
        trade = self.get_trade_offer(trade_offer_id, use_webtoken=True)
        trade_offer_state = TradeOfferState(
            trade["response"]["offer"]["trade_offer_state"]
        )
        if trade_offer_state is not TradeOfferState.Active:
            raise ApiException(
                f"Invalid trade offer state: {trade_offer_state.name} "
                f"({trade_offer_state.value})"
            )

        partner = self._fetch_trade_partner_id(trade_offer_id)
        accept_url = f"{self._get_trade_offer_url(trade_offer_id)}/accept"
        params = {
            "sessionid": self._get_session_id(),
            "tradeofferid": trade_offer_id,
            "serverid": "1",
            "partner": partner,
            "captcha": "",
        }
        headers = {"Referer": self._get_trade_offer_url(trade_offer_id)}

        response = self._session.post(accept_url, data=params, headers=headers).json()
        if response.get("needs_mobile_confirmation", False):
            return self._confirm_transaction(trade_offer_id)
        return response

    def _fetch_trade_partner_id(self, trade_offer_id: str) -> str:
        url = self._get_trade_offer_url(trade_offer_id)
        offer_response_text = self._session.get(url).text
        if (
            "You have logged in from a new device. In order to protect the items"
            in offer_response_text
        ):
            raise SevenDaysHoldException(
                "Account has logged in a new device and can't trade for 7 days"
            )
        return text_between(
            offer_response_text, "var g_ulTradePartnerSteamID = '", "';"
        )

    def _confirm_transaction(self, trade_offer_id: str) -> dict:
        assert self.steam_guard is not None
        confirmation_executor = ConfirmationExecutor(
            self.steam_guard["identity_secret"],
            self.steam_guard["steamid"],
            self._session,
        )
        return confirmation_executor.send_trade_allow_request(trade_offer_id)

    def decline_trade_offer(self, trade_offer_id: str) -> dict:
        """Decline an incoming trade offer."""
        url = f"{self._get_trade_offer_url(trade_offer_id)}/decline"
        return self._session.post(
            url, data={"sessionid": self._get_session_id()}
        ).json()

    def cancel_trade_offer(self, trade_offer_id: str) -> dict:
        """Cancel an outgoing trade offer."""
        url = f"{self._get_trade_offer_url(trade_offer_id)}/cancel"
        return self._session.post(
            url, data={"sessionid": self._get_session_id()}
        ).json()

    @login_required
    def make_offer(
        self,
        items_from_me: list[Asset],
        items_from_them: list[Asset],
        partner_steam_id: str,
        message: str = "",
    ) -> dict:
        """Send a trade offer to ``partner_steam_id`` by their steam id."""
        offer = self._create_offer_dict(items_from_me, items_from_them)
        url = f"{SteamUrl.COMMUNITY_URL}/tradeoffer/new/send"
        params = {
            "sessionid": self._get_session_id(),
            "serverid": _TRADE_SERVER_ID,
            "partner": partner_steam_id,
            "tradeoffermessage": message,
            "json_tradeoffer": json.dumps(offer),
            "captcha": "",
            "trade_offer_create_params": "{}",
        }
        partner_account_id = steam_id_to_account_id(partner_steam_id)
        headers = {
            "Referer": (
                f"{SteamUrl.COMMUNITY_URL}/tradeoffer/new/?partner={partner_account_id}"
            ),
            "Origin": SteamUrl.COMMUNITY_URL,
        }

        response = self._session.post(url, data=params, headers=headers).json()
        if response.get("needs_mobile_confirmation"):
            response.update(self._confirm_transaction(response["tradeofferid"]))
        return response

    def get_profile(self, steam_id: str) -> dict:
        """Return the public profile summary for ``steam_id``."""
        params = {"steamids": steam_id, "key": self._api_key}
        response = self.api_call(
            "GET", "ISteamUser", "GetPlayerSummaries", "v0002", params
        )
        return response.json()["response"]["players"][0]

    def get_friend_list(self, steam_id: str, relationship_filter: str = "all") -> dict:
        """Return the friend list for ``steam_id``."""
        params = {
            "key": self._api_key,
            "steamid": steam_id,
            "relationship": relationship_filter,
        }
        response = self.api_call("GET", "ISteamUser", "GetFriendList", "v1", params)
        return response.json()["friendslist"]["friends"]

    @staticmethod
    def _create_offer_dict(
        items_from_me: list[Asset], items_from_them: list[Asset]
    ) -> dict:
        return {
            "newversion": True,
            "version": 4,
            "me": {
                "assets": [asset.to_dict() for asset in items_from_me],
                "currency": [],
                "ready": False,
            },
            "them": {
                "assets": [asset.to_dict() for asset in items_from_them],
                "currency": [],
                "ready": False,
            },
        }

    @login_required
    def get_escrow_duration(self, trade_offer_url: str) -> int:
        """Return the maximum escrow hold (in days) for a trade-offer URL."""
        referer = f"{SteamUrl.COMMUNITY_URL}{urlparse.urlparse(trade_offer_url).path}"
        headers = {"Referer": referer, "Origin": SteamUrl.COMMUNITY_URL}
        response = self._session.get(trade_offer_url, headers=headers).text
        my_escrow_duration = int(text_between(response, "var g_daysMyEscrow = ", ";"))
        their_escrow_duration = int(
            text_between(response, "var g_daysTheirEscrow = ", ";")
        )
        return max(my_escrow_duration, their_escrow_duration)

    @login_required
    def make_offer_with_url(
        self,
        items_from_me: list[Asset],
        items_from_them: list[Asset],
        trade_offer_url: str,
        message: str = "",
        case_sensitive: bool = True,
        confirm_trade: bool = True,
    ) -> dict:
        """Send a trade offer using a partner's public trade-offer URL."""
        token = get_key_value_from_url(trade_offer_url, "token", case_sensitive)
        partner_account_id = get_key_value_from_url(
            trade_offer_url, "partner", case_sensitive
        )
        partner_steam_id = account_id_to_steam_id(partner_account_id)
        offer = self._create_offer_dict(items_from_me, items_from_them)
        url = f"{SteamUrl.COMMUNITY_URL}/tradeoffer/new/send"
        referer = f"{SteamUrl.COMMUNITY_URL}{urlparse.urlparse(trade_offer_url).path}"
        trade_offer_create_params = {"trade_offer_access_token": token}
        params = {
            "sessionid": self._get_session_id(),
            "serverid": _TRADE_SERVER_ID,
            "partner": partner_steam_id,
            "tradeoffermessage": message,
            "json_tradeoffer": json.dumps(offer),
            "captcha": "",
            "trade_offer_create_params": json.dumps(trade_offer_create_params),
        }
        headers = {
            "Referer": referer,
            "Origin": SteamUrl.COMMUNITY_URL,
        }

        response = self._session.post(url, data=params, headers=headers).json()
        if confirm_trade and response.get("needs_mobile_confirmation"):
            response.update(self._confirm_transaction(response["tradeofferid"]))
        return response

    @staticmethod
    def _get_trade_offer_url(trade_offer_id: str) -> str:
        return f"{SteamUrl.COMMUNITY_URL}/tradeoffer/{trade_offer_id}"

    @login_required
    def get_wallet_info(self) -> dict:
        """Return the full ``g_rgWalletInfo`` dict from the market page.

        Includes the wallet currency code, the balance and delayed balance (both
        in minor units, e.g. cents), the wallet country and the fee settings.
        """
        response = self._session.get(f"{SteamUrl.COMMUNITY_URL}/market")
        wallet_info_match = re.search(r"var g_rgWalletInfo = (.*?);", response.text)
        if not wallet_info_match:
            raise ApiException("Unable to get wallet info from the market page")
        return json.loads(wallet_info_match.group(1))

    @login_required
    def get_wallet_balance(
        self, convert_to_decimal: bool = True, on_hold: bool = False
    ) -> str | Decimal:
        """Return the wallet balance.

        Args:
            convert_to_decimal: When ``True`` return a :class:`~decimal.Decimal`
                in major units; when ``False`` return the raw integer string.
            on_hold: When ``True`` return the delayed (on-hold) balance instead.
        """
        wallet_info = self.get_wallet_info()
        balance_key = "wallet_delayed_balance" if on_hold else "wallet_balance"
        if convert_to_decimal:
            return Decimal(wallet_info[balance_key]) / 100
        return wallet_info[balance_key]
