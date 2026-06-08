import asyncio

import pytest

from steampy import async_market
from steampy.async_market import AsyncMarket, _proxy_url
from steampy.models import GameOptions


class _FakeResponseCtx:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeResponseCtx":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def json(self) -> dict:
        return self._payload


class _FakeSession:
    def __init__(self, **kwargs: object) -> None:
        self.cookies = kwargs.get("cookies")
        self.get_calls: list = []
        self.post_calls: list = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def get(self, url: str, params: dict, proxy: str | None = None) -> _FakeResponseCtx:
        self.get_calls.append((url, params, proxy))
        return _FakeResponseCtx({"name": params["market_hash_name"]})

    def post(
        self, url: str, data: dict, headers: dict, proxy: str | None = None
    ) -> _FakeResponseCtx:
        self.post_calls.append((url, data, proxy))
        return _FakeResponseCtx({"success": 1})


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list:
    created: list = []

    def factory(**kwargs: object) -> _FakeSession:
        session = _FakeSession(**kwargs)
        created.append(session)
        return session

    monkeypatch.setattr(async_market.aiohttp, "ClientSession", factory)
    return created


def test_proxy_url() -> None:
    assert _proxy_url(None) is None
    assert _proxy_url({"http": "http://p"}) == "http://p"
    assert _proxy_url({"https": "http://s", "http": "http://p"}) == "http://s"


def test_fetch_prices_concurrently(sessions: list) -> None:
    market = AsyncMarket(
        "sess", {"steamLoginSecure": "x"}, proxy={"https": "http://proxy"}
    )
    result = asyncio.run(market.fetch_prices(["A", "B"], GameOptions.CS))

    assert result == {"A": {"name": "A"}, "B": {"name": "B"}}
    session = sessions[0]
    assert session.cookies == {"steamLoginSecure": "x"}
    assert [call[2] for call in session.get_calls] == ["http://proxy", "http://proxy"]


def test_create_buy_orders_concurrently(sessions: list) -> None:
    market = AsyncMarket("sess", {})
    orders = [("AK-47", "1.00", 1), ("M4A4", "2.00", 2)]
    result = asyncio.run(market.create_buy_orders(orders, GameOptions.CS))

    assert result == [{"success": 1}, {"success": 1}]
    posted = sessions[0].post_calls
    assert len(posted) == 2
    assert posted[0][1]["market_hash_name"] == "AK-47"
    assert posted[1][1]["price_total"] == "4.00"  # 2.00 * 2
