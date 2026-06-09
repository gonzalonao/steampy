"""A requests Session that round-robins and fails over across a proxy pool."""

from __future__ import annotations

import logging
import random
import threading

import requests
from requests.exceptions import ProxyError

from steampy.exceptions import ProxyConnectionError
from steampy.utils import ping_proxy, retry_call

logger = logging.getLogger(__name__)


class RotatingProxySession(requests.Session):
    """A :class:`requests.Session` with round-robin proxy rotation.

    :meth:`rotating_get` and :meth:`rotating_post` pick the next proxy from the
    configured pool for every call and automatically fail over to the next proxy
    on a :class:`~requests.exceptions.ProxyError`. With no pool configured they
    fall back to plain requests, so the session is a drop-in replacement for a
    regular :class:`requests.Session`.

    Rotation state is guarded by a lock, so a single instance can be shared
    safely across threads.
    """

    def __init__(self) -> None:
        super().__init__()
        self._proxies_list: list[dict] = []
        self._index = 0
        self._lock = threading.Lock()

    def set_proxies_list(
        self, proxies_list: list[dict], skip_ping: bool = False
    ) -> None:
        """Configure the proxy pool to rotate over.

        Args:
            proxies_list: Proxy dicts shaped like ``requests.Session.proxies``.
            skip_ping: When ``False`` (default) each proxy is validated against
                Steam and unreachable ones are dropped; when ``True`` the list
                is trusted as-is (useful when it was validated upstream).

        Raises:
            TypeError: If ``proxies_list`` is not a list.
            ProxyConnectionError: If validation leaves no reachable proxy.
        """
        if not isinstance(proxies_list, list):
            raise TypeError("proxies_list must be a list of proxy dicts")
        if not proxies_list:
            raise ProxyConnectionError("proxies_list cannot be empty")

        if skip_ping:
            self._proxies_list = list(proxies_list)
        else:
            self._proxies_list = [proxy for proxy in proxies_list if ping_proxy(proxy)]
            if not self._proxies_list:
                raise ProxyConnectionError("No reachable proxies in the provided list")

        # Randomise the starting offset so several workers do not all begin on
        # the same proxy.
        self._index = random.randint(0, len(self._proxies_list) - 1)

    def _next_proxy(self) -> dict:
        with self._lock:
            proxy = self._proxies_list[self._index % len(self._proxies_list)]
            self._index = (self._index + 1) % len(self._proxies_list)
        return proxy

    def rotating_get(self, url: str, **kwargs: object) -> requests.Response:
        """Send a GET using the next proxy, failing over on proxy errors."""
        return self._rotating_request("GET", url, **kwargs)

    def rotating_post(
        self, url: str, data: object = None, **kwargs: object
    ) -> requests.Response:
        """Send a POST using the next proxy, failing over on proxy errors."""
        return self._rotating_request("POST", url, data=data, **kwargs)

    def _rotating_request(
        self, method: str, url: str, **kwargs: object
    ) -> requests.Response:
        # No pool, or the caller pinned a proxy explicitly: behave like a plain
        # session and let requests handle it.
        if not self._proxies_list or "proxies" in kwargs:
            return self.request(method, url, **kwargs)  # type: ignore[arg-type]

        attempts = len(self._proxies_list)

        def attempt() -> requests.Response:
            proxy = self._next_proxy()
            logger.debug("Sending %s %s via proxy %s", method, url, proxy)
            return self.request(method, url, proxies=proxy, **kwargs)  # type: ignore[arg-type]

        try:
            return retry_call(
                attempt, attempts=attempts, retry_exceptions=(ProxyError,)
            )
        except ProxyError as exc:
            raise ProxyError(f"All {attempts} proxy attempts failed for {url}") from exc
