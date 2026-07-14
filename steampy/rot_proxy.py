import random
import requests

from requests.exceptions import ProxyError
from steampy.models import DEFAULT_HEADERS
from steampy.utils import ping_proxy


def _proxy_host(proxy: dict | None) -> str:
    """Return the proxy's host:port for logging, stripping credentials."""
    if not proxy:
        return 'none'
    url = proxy.get('https') or proxy.get('http') or ''
    return url.rsplit('@', 1)[-1] or 'unknown'


class RotatingProxySession(requests.Session):
    # static/class variable to remember last-used proxy index across instances
    _last_proxy_index = 0

    def __init__(self):
        super().__init__()
        self.headers.update(DEFAULT_HEADERS)
        self._proxies_list = []

    def set_proxies_list(self, proxies_list: list[dict], skip_ping: bool = True) -> None:
        """
        Accepts a list of proxy dicts (same shape as requests.Session.proxies).
        Stores the list and sets the session.proxies to the first available proxy.
        If skip_ping=True (default), assumes proxies have already been validated.
        """
        if not isinstance(proxies_list, list):
            raise TypeError('proxies_list must be a list of proxy dicts')
        if not proxies_list:
            raise ValueError('proxies_list cannot be empty')
        
        if skip_ping:
            # Assume proxies are already validated
            self._proxies_list = proxies_list
        else:
            # filter reachable proxies (legacy behavior)
            working = [p for p in proxies_list if ping_proxy(p)]
            if not working:
                raise ValueError('No reachable proxies in provided list')
            self._proxies_list = working
        
        # Randomize starting position so concurrent processes don't all hammer proxy 0
        RotatingProxySession._last_proxy_index = random.randint(0, len(self._proxies_list) - 1)
        self.proxies.update(self._proxies_list[RotatingProxySession._last_proxy_index])

    def _pick_next_proxy(self) -> dict | None:
        if not self._proxies_list:
            return None
        idx = RotatingProxySession._last_proxy_index % len(self._proxies_list)
        proxy = self._proxies_list[idx]
        # update the static index so next call uses the next proxy
        RotatingProxySession._last_proxy_index = (idx + 1) % len(self._proxies_list)
        return proxy

    def rotating_get(self, url: str, max_proxy_retries: int = None, **kwargs) -> requests.Response:
        """
        Send a GET using the next proxy from the list (if any).
        Falls back to normal GET when no proxy list is configured.

        On a ProxyError, automatically retries with the next proxy in the list.
        The number of retries is capped at the total number of available proxies
        (or max_proxy_retries if provided) to avoid infinite loops.
        """
        if not self._proxies_list:
            print('[DEBUG] No proxies configured, using direct connection')
            return super().get(url, **kwargs)

        # How many times we're willing to retry on proxy failure
        retries_allowed = max_proxy_retries if max_proxy_retries is not None else len(self._proxies_list)

        last_exc = None
        for attempt in range(retries_allowed + 1):  # +1 for the initial attempt
            proxy = self._pick_next_proxy()
            # Allow an explicit 'proxies' kwarg to override only on the first attempt
            attempt_kwargs = dict(kwargs)
            if attempt == 0:
                attempt_kwargs.setdefault('proxies', proxy)
            else:
                attempt_kwargs['proxies'] = proxy  # force the new proxy on retries

            try:
                return super().get(url, **attempt_kwargs)
            except ProxyError as e:
                last_exc = e
                print(
                    f'[DEBUG] ProxyError on attempt {attempt + 1}/{retries_allowed + 1} '
                    f'(proxy: {_proxy_host(proxy)}). Retrying with next proxy...'
                )

        raise ProxyError(
            f'All {retries_allowed + 1} proxy attempts failed for {url}. Last error: {last_exc}'
        ) from last_exc

    def rotating_post(self, url: str, data=None, max_proxy_retries: int = None, **kwargs) -> requests.Response:
        """
        Send a POST using the next proxy from the list (if any).
        Falls back to normal POST when no proxy list is configured.

        On a ProxyError, automatically retries with the next proxy in the list.
        The number of retries is capped at the total number of available proxies
        (or max_proxy_retries if provided) to avoid infinite loops.
        """
        if not self._proxies_list:
            print('[DEBUG] No proxies configured, using direct connection')
            return super().post(url, data=data, **kwargs)

        retries_allowed = max_proxy_retries if max_proxy_retries is not None else len(self._proxies_list)

        last_exc = None
        for attempt in range(retries_allowed + 1):  # +1 for the initial attempt
            proxy = self._pick_next_proxy()
            attempt_kwargs = dict(kwargs)
            if attempt == 0:
                attempt_kwargs.setdefault('proxies', proxy)
            else:
                attempt_kwargs['proxies'] = proxy  # force the new proxy on retries

            try:
                return super().post(url, data=data, **attempt_kwargs)
            except ProxyError as e:
                last_exc = e
                print(
                    f'[DEBUG] ProxyError on POST attempt {attempt + 1}/{retries_allowed + 1} '
                    f'(proxy: {_proxy_host(proxy)}). Retrying with next proxy...'
                )

        raise ProxyError(
            f'All {retries_allowed + 1} proxy POST attempts failed for {url}. Last error: {last_exc}'
        ) from last_exc