"""Perform the Steam web login flow and establish an authenticated session."""

from __future__ import annotations

from base64 import b64encode
from http import HTTPStatus
from typing import TYPE_CHECKING

from rsa import PublicKey, encrypt

from steampy import guard
from steampy.exceptions import ApiException, CaptchaRequired
from steampy.models import SteamUrl
from steampy.utils import create_cookie

if TYPE_CHECKING:
    from requests import Response, Session

# Steam confirms the guard code with code type 3 (mobile authenticator).
_GUARD_CODE_TYPE = 3
_MAX_RSA_FETCH_RETRIES = 5
# Cookies copied to both the community and store domains after login.
_SHARED_COOKIE_NAMES = (
    "steamLoginSecure",
    "sessionid",
    "steamRefresh_steam",
    "steamCountry",
)


class LoginExecutor:
    """Drive the credentials-based login against Steam's auth service."""

    def __init__(
        self, username: str, password: str, shared_secret: str, session: Session
    ) -> None:
        self.username = username
        self.password = password
        self.shared_secret = shared_secret
        self.session = session
        self.refresh_token = ""

    def _api_call(
        self,
        method: str,
        service: str,
        endpoint: str,
        version: str = "v1",
        params: dict | None = None,
    ) -> Response:
        url = f"{SteamUrl.API_URL}/{service}/{endpoint}/{version}"
        # All requests from the login page share the same Referer and Origin.
        headers = {
            "Referer": f"{SteamUrl.COMMUNITY_URL}/",
            "Origin": SteamUrl.COMMUNITY_URL,
        }
        if method.upper() == "GET":
            return self.session.get(url, params=params, headers=headers)
        if method.upper() == "POST":
            return self.session.post(url, data=params, headers=headers)
        raise ValueError("Method must be either GET or POST")

    def login(self) -> Session:
        """Run the full login flow and return the authenticated session."""
        login_response = self._send_login_request()
        if not login_response.json()["response"]:
            raise ApiException(
                "No response received from Steam API. Please try again later."
            )
        self._check_for_captcha(login_response)
        self._update_steam_guard(login_response)
        finalized_response = self._finalize_login()
        self._perform_redirects(finalized_response.json())
        self.set_sessionid_cookies()
        return self.session

    def _send_login_request(self) -> Response:
        rsa_params = self._fetch_rsa_params()
        encrypted_password = self._encrypt_password(rsa_params)
        rsa_timestamp = rsa_params["rsa_timestamp"]
        request_data = self._prepare_login_request_data(
            encrypted_password, rsa_timestamp
        )
        return self._api_call(
            "POST",
            "IAuthenticationService",
            "BeginAuthSessionViaCredentials",
            params=request_data,
        )

    def set_sessionid_cookies(self) -> None:
        """Mirror the session cookies onto both the community and store domains."""
        community_domain = SteamUrl.COMMUNITY_URL[len("https://") :]
        store_domain = SteamUrl.STORE_URL[len("https://") :]
        community_cookies = self.session.cookies.get_dict(domain=community_domain)
        store_cookies = self.session.cookies.get_dict(domain=store_domain)
        all_cookies = self.session.cookies.get_dict()

        for name in _SHARED_COOKIE_NAMES:
            # steamLoginSecure and sessionid are domain-specific, so copy each
            # domain's own value; the rest are shared verbatim.
            store_value = (
                store_cookies[name] if name == "steamLoginSecure" else all_cookies[name]
            )
            community_value = (
                community_cookies[name]
                if name in ("sessionid", "steamLoginSecure")
                else all_cookies[name]
            )
            self.session.cookies.set(
                **create_cookie(name, community_value, community_domain)
            )
            self.session.cookies.set(**create_cookie(name, store_value, store_domain))

    def _fetch_rsa_params(self, current_number_of_repetitions: int = 0) -> dict:
        self.session.post(SteamUrl.COMMUNITY_URL)
        request_data = {"account_name": self.username}
        response = self._api_call(
            "GET",
            "IAuthenticationService",
            "GetPasswordRSAPublicKey",
            params=request_data,
        )

        if response.status_code == HTTPStatus.OK and "response" in response.json():
            key_data = response.json()["response"]
            # Steam may return an empty 'response' value even with a 200 status.
            if (
                "publickey_mod" in key_data
                and "publickey_exp" in key_data
                and "timestamp" in key_data
            ):
                rsa_mod = int(key_data["publickey_mod"], 16)
                rsa_exp = int(key_data["publickey_exp"], 16)
                return {
                    "rsa_key": PublicKey(rsa_mod, rsa_exp),
                    "rsa_timestamp": key_data["timestamp"],
                }

        if current_number_of_repetitions < _MAX_RSA_FETCH_RETRIES:
            return self._fetch_rsa_params(current_number_of_repetitions + 1)

        raise ApiException(
            f"Could not obtain rsa-key. Status code: {response.status_code}"
        )

    def _encrypt_password(self, rsa_params: dict) -> bytes:
        return b64encode(encrypt(self.password.encode("utf-8"), rsa_params["rsa_key"]))

    def _prepare_login_request_data(
        self, encrypted_password: bytes, rsa_timestamp: str
    ) -> dict:
        return {
            "persistence": "1",
            "encrypted_password": encrypted_password,
            "account_name": self.username,
            "encryption_timestamp": rsa_timestamp,
        }

    @staticmethod
    def _check_for_captcha(login_response: Response) -> None:
        if login_response.json().get("captcha_needed", False):
            raise CaptchaRequired("Captcha required")

    def _perform_redirects(self, response_dict: dict) -> None:
        parameters = response_dict.get("transfer_info")
        if parameters is None:
            raise ApiException(
                "Cannot perform redirects after login, no parameters fetched"
            )
        for pass_data in parameters:
            pass_data["params"]["steamID"] = response_dict["steamID"]
            self.session.post(pass_data["url"], pass_data["params"])

    def _update_steam_guard(self, login_response: Response) -> None:
        response = login_response.json()["response"]
        client_id = response["client_id"]
        steamid = response["steamid"]
        request_id = response["request_id"]
        code = guard.generate_one_time_code(self.shared_secret)

        update_data = {
            "client_id": client_id,
            "steamid": steamid,
            "code_type": _GUARD_CODE_TYPE,
            "code": code,
        }
        update_response = self._api_call(
            "POST",
            "IAuthenticationService",
            "UpdateAuthSessionWithSteamGuardCode",
            params=update_data,
        )
        if update_response.status_code != HTTPStatus.OK:
            raise ApiException("Cannot update Steam guard")
        self._pool_sessions_steam(client_id, request_id)

    def _pool_sessions_steam(self, client_id: str, request_id: str) -> None:
        pool_data = {"client_id": client_id, "request_id": request_id}
        response = self._api_call(
            "POST", "IAuthenticationService", "PollAuthSessionStatus", params=pool_data
        )
        self.refresh_token = response.json()["response"]["refresh_token"]

    def _finalize_login(self) -> Response:
        sessionid = self.session.cookies["sessionid"]
        redir = f"{SteamUrl.COMMUNITY_URL}/login/home/?goto="
        finalized_data = {
            "nonce": self.refresh_token,
            "sessionid": sessionid,
            "redir": redir,
        }
        return self.session.post(
            SteamUrl.LOGIN_URL + "/jwt/finalizelogin", data=finalized_data
        )
