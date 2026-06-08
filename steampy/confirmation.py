"""Handle Steam mobile confirmations for trade offers and market listings."""

from __future__ import annotations

import enum
import json
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from steampy import guard
from steampy.exceptions import ConfirmationExpected, InvalidCredentials

if TYPE_CHECKING:
    import requests


class Confirmation:
    """A pending mobile confirmation identified by its id and nonce."""

    def __init__(self, data_confid: str, nonce: str) -> None:
        self.data_confid = data_confid
        self.nonce = nonce


class Tag(enum.Enum):
    """Confirmation request tags expected by the mobile endpoints."""

    CONF = "conf"
    ALLOW = "allow"


class ConfirmationExecutor:
    """Fetch and answer the mobile confirmations queued for an account."""

    CONF_URL = "https://steamcommunity.com/mobileconf"

    def __init__(
        self, identity_secret: str, my_steam_id: str, session: requests.Session
    ) -> None:
        self._my_steam_id = my_steam_id
        self._identity_secret = identity_secret
        self._session = session

    def send_trade_allow_request(self, trade_offer_id: str) -> dict:
        """Confirm the pending trade offer with the given id."""
        confirmations = self._get_confirmations()
        confirmation = self._select_confirmation(
            confirmations,
            trade_offer_id,
            self._get_confirmation_trade_offer_id,
        )
        return self._send_confirmation(confirmation)

    def confirm_sell_listing(self, asset_id: str) -> dict:
        """Confirm the pending sell listing for the given asset id."""
        confirmations = self._get_confirmations()
        confirmation = self._select_confirmation(
            confirmations,
            asset_id,
            self._get_confirmation_sell_listing_id,
        )
        return self._send_confirmation(confirmation)

    def _send_confirmation(self, confirmation: Confirmation) -> dict:
        tag = Tag.ALLOW
        params = self._create_confirmation_params(tag.value)
        params["op"] = tag.value
        params["cid"] = confirmation.data_confid
        params["ck"] = confirmation.nonce
        headers = {"X-Requested-With": "XMLHttpRequest"}
        return self._session.get(
            f"{self.CONF_URL}/ajaxop", params=params, headers=headers
        ).json()

    def _get_confirmations(self) -> list[Confirmation]:
        confirmations_page = self._fetch_confirmations_page()
        if confirmations_page.status_code != HTTPStatus.OK:
            raise ConfirmationExpected
        confirmations_json = json.loads(confirmations_page.text)
        return [
            Confirmation(conf["id"], conf["nonce"])
            for conf in confirmations_json["conf"]
        ]

    def _fetch_confirmations_page(self) -> requests.Response:
        params = self._create_confirmation_params(Tag.CONF.value)
        headers = {"X-Requested-With": "com.valvesoftware.android.steam.community"}
        response = self._session.get(
            f"{self.CONF_URL}/getlist", params=params, headers=headers
        )
        if (
            "Steam Guard Mobile Authenticator is providing incorrect Steam Guard codes."
            in response.text
        ):
            raise InvalidCredentials("Invalid Steam Guard file")
        return response

    def _fetch_confirmation_details_page(self, confirmation: Confirmation) -> str:
        tag = f"details{confirmation.data_confid}"
        params = self._create_confirmation_params(tag)
        response = self._session.get(
            f"{self.CONF_URL}/details/{confirmation.data_confid}", params=params
        )
        return response.json()["html"]

    def _create_confirmation_params(self, tag_string: str) -> dict:
        timestamp = int(time.time())
        confirmation_key = guard.generate_confirmation_key(
            self._identity_secret, tag_string, timestamp
        )
        android_id = guard.generate_device_id(self._my_steam_id)
        return {
            "p": android_id,
            "a": self._my_steam_id,
            "k": confirmation_key,
            "t": timestamp,
            "m": "android",
            "tag": tag_string,
        }

    def _select_confirmation(
        self,
        confirmations: list[Confirmation],
        target_id: str,
        id_getter: Callable[[str], str],
    ) -> Confirmation:
        """Return the confirmation whose details page matches ``target_id``.

        Args:
            confirmations: The confirmations currently queued for the account.
            target_id: The trade-offer or asset id being confirmed.
            id_getter: Extractor that reads the relevant id from a details page.
        """
        for confirmation in confirmations:
            details_page = self._fetch_confirmation_details_page(confirmation)
            if id_getter(details_page) == target_id:
                return confirmation
        raise ConfirmationExpected

    @staticmethod
    def _get_confirmation_sell_listing_id(confirmation_details_page: str) -> str:
        soup = BeautifulSoup(confirmation_details_page, "html.parser")
        scr_raw = (soup.select("script")[2].string or "").strip()
        scr_raw = scr_raw[scr_raw.index("'confiteminfo', ") + 16 :]
        scr_raw = scr_raw[: scr_raw.index(", UserYou")].replace("\n", "")
        return json.loads(scr_raw)["id"]

    @staticmethod
    def _get_confirmation_trade_offer_id(confirmation_details_page: str) -> str:
        soup = BeautifulSoup(confirmation_details_page, "html.parser")
        full_offer_id = str(soup.select(".tradeoffer")[0]["id"])
        return full_offer_id.split("_")[1]
