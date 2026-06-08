"""Helpers to bootstrap several clients and a proxy pool at once."""

from __future__ import annotations

import logging

import requests

from steampy.client import SteamClient
from steampy.exceptions import ProxyConnectionError
from steampy.utils import ping_proxy

logger = logging.getLogger(__name__)


def load_clients(
    accounts: dict[str, dict],
    api_key: str = "",
    proxies: dict | list[dict] | None = None,
) -> dict[str, SteamClient]:
    """Create one cookie-authenticated :class:`SteamClient` per account.

    Args:
        accounts: Mapping of account name to its login-cookie dict.
        api_key: Shared Web API key; leave empty for cookie-only workflows.
        proxies: A single proxy dict or a pool shared by every client.

    Returns:
        A mapping of account name to a ready-to-use client.
    """
    clients: dict[str, SteamClient] = {}
    for name, cookies in accounts.items():
        logger.debug("Creating client for account %s", name)
        clients[name] = SteamClient(api_key, login_cookies=cookies, proxies=proxies)
    return clients


def fetch_proxies(url: str, validate: bool = True, timeout: int = 10) -> list[dict]:
    """Fetch a proxy pool from a URL of ``ip:port:user:password`` lines.

    Args:
        url: Endpoint returning one ``ip:port:user:password`` proxy per line.
        validate: When ``True``, drop proxies that cannot reach Steam.
        timeout: Timeout, in seconds, for fetching the proxy list.

    Returns:
        A list of ``requests``-style proxy dicts.

    Raises:
        requests.RequestException: If the proxy list cannot be fetched.
        ProxyConnectionError: If validation leaves no reachable proxy.
    """
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()

    proxies: list[dict] = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ip, port, user, password = line.split(":")
        except ValueError:
            logger.warning("Skipping malformed proxy line: %s", line)
            continue
        endpoint = f"http://{user}:{password}@{ip}:{port}"
        proxies.append({"http": endpoint, "https": endpoint})

    if not validate:
        return proxies

    working = [proxy for proxy in proxies if ping_proxy(proxy)]
    logger.debug("%d of %d proxies are reachable", len(working), len(proxies))
    if not working:
        raise ProxyConnectionError("No reachable proxies found from the provided URL")
    return working
