# steampy — Steam trade & market automation

[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE.txt)
[![Linting: Ruff](https://img.shields.io/badge/lint-ruff-orange.svg)](https://github.com/astral-sh/ruff)
[![Types: mypy strict](https://img.shields.io/badge/types-mypy%20strict-blue.svg)](https://mypy-lang.org/)

A typed Python library for automating Steam trading and Steam Community Market
operations — logging in, handling trade offers, reading inventories, and placing
and tracking market orders.

> **About this fork.** This is my fork of [`bukson/steampy`](https://github.com/bukson/steampy).
> On top of the upstream library I added **rotating-proxy support**, a **concurrent
> async market layer**, **full market-history retrieval**, and **multi-account
> bootstrapping**, then refactored the whole codebase to a strict-typed, fully
> tested and linted state. It powers a personal Steam Market data pipeline and
> analytics dashboard. See [Changes from upstream](#changes-from-upstream).

---

## Features

- **Authentication** with username + password + Steam Guard, or with existing
  session cookies.
- **Trading** — fetch, accept, decline, cancel and create trade offers, with
  automatic mobile confirmation.
- **Inventory** — read your own and a partner's inventory.
- **Market** — prices, price history, your listings, buy/sell orders and wallet info.
- 🔁 **Rotating proxies** — round-robin a pool of proxies with automatic
  failover on proxy errors, transparent to every market and inventory call.
- ⚡ **Async market layer** — place many buy orders or fetch many prices
  concurrently over `aiohttp`, reusing the same request builders as the sync code.
- 📊 **Market history** — paginate and parse your full transaction history with
  built-in retry on rate limits.
- 👥 **Multi-account** — bootstrap one client per account and load a validated
  proxy pool from a URL.

## Installation

```bash
pip install git+https://github.com/gonzalonao/steampy.git
```

For the concurrent async layer, install the optional extra:

```bash
pip install "steampy[async] @ git+https://github.com/gonzalonao/steampy.git"
```

Requires Python 3.12+.

## Quick start

```python
from steampy.client import SteamClient
from steampy.models import GameOptions

client = SteamClient(api_key="YOUR_API_KEY")
client.login("username", "password", "path/to/Steamguard.txt")

inventory = client.get_my_inventory(GameOptions.CS)
print(f"{len(inventory)} items in inventory")
```

You can also authenticate with existing cookies (no password needed):

```python
client = SteamClient(
    api_key="YOUR_API_KEY",
    login_cookies={"sessionid": "...", "steamLoginSecure": "...", "steamCountry": "..."},
)
```

## Highlights

### Rotating proxies

```python
proxies = [
    {"http": "http://user:pass@host1:port", "https": "http://user:pass@host1:port"},
    {"http": "http://user:pass@host2:port", "https": "http://user:pass@host2:port"},
]
client = SteamClient(api_key, proxies=proxies)  # a single dict also works
```

Market and inventory calls now round-robin over the pool and fail over
automatically when a proxy stops responding.

### Concurrent buy orders (async)

```python
import asyncio
from steampy.async_market import AsyncMarket
from steampy.models import Currency, GameOptions

market = AsyncMarket.from_client(client)
orders = [("AK-47 | Redline (Field-Tested)", "10.00", 1)]
asyncio.run(market.create_buy_orders(orders, GameOptions.CS, Currency.USD))
```

### Full market history

```python
history = client.market.get_market_history()  # paginated + rate-limit retries
```

### Multiple accounts

```python
from steampy.accounts import fetch_proxies, load_clients

proxies = fetch_proxies("https://example.com/proxies.txt")
clients = load_clients(accounts={"main": cookies_dict}, proxies=proxies)
```

More runnable scripts live in [`examples/`](examples).

## Development

```bash
pip install -e ".[dev]"
ruff format --check . && ruff check .   # format + lint
mypy steampy                            # strict type checking
pytest                                  # offline unit tests
```

The credential/network acceptance tests under `test/` are skipped by default;
the rest run fully offline.

## Changes from upstream

This fork adds the following on top of [`bukson/steampy`](https://github.com/bukson/steampy):

- **`RotatingProxySession`** (`steampy/session.py`) — round-robin proxy rotation
  with automatic failover, integrated non-breakingly into `SteamClient`.
- **`AsyncMarket`** (`steampy/async_market.py`) — a thin `aiohttp` layer for
  concurrent price fetches and buy orders, sharing request builders with the
  synchronous market so there is no duplicated logic.
- **`SteamMarket.get_market_history()`** and **`SteamClient.get_wallet_info()`**.
- **`steampy/accounts.py`** — `load_clients()` and `fetch_proxies()` helpers for
  multi-account workflows.
- A library-wide cleanup: removed dead code, deduplicated repeated logic, added
  a reusable `retry_call` helper, locale-robust price parsing, Google-style
  docstrings, `mypy --strict` typing, a `pyproject.toml` toolchain (Ruff + mypy),
  a `py.typed` marker and a CI quality gate.

The upstream README documents the full method reference. The cookie-first variant
used for my personal pipeline lives on the
[`personal/cookie-first`](https://github.com/gonzalonao/steampy/tree/personal/cookie-first)
branch.

## Credits & license

Originally created by Michał Bukowski ([`bukson/steampy`](https://github.com/bukson/steampy))
and released under the MIT License. This fork is maintained by
**Gonzalo López Crespo** and remains under the same [MIT License](LICENSE.txt).

- GitHub: [@gonzalonao](https://github.com/gonzalonao)
- LinkedIn: [gonzalolopezcrespo](https://linkedin.com/in/gonzalolopezcrespo)
