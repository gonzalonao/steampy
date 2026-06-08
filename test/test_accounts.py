import pytest

from steampy import accounts
from steampy.exceptions import ProxyConnectionError

PROXY_LIST = "1.1.1.1:8000:user:pass\n2.2.2.2:9000:user:pass\nmalformed-line\n"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def test_fetch_proxies_parses_and_skips_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        accounts.requests, "get", lambda *a, **k: _FakeResponse(PROXY_LIST)
    )

    proxies = accounts.fetch_proxies("https://example.com/proxies", validate=False)

    assert proxies == [
        {
            "http": "http://user:pass@1.1.1.1:8000",
            "https": "http://user:pass@1.1.1.1:8000",
        },
        {
            "http": "http://user:pass@2.2.2.2:9000",
            "https": "http://user:pass@2.2.2.2:9000",
        },
    ]


def test_fetch_proxies_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        accounts.requests, "get", lambda *a, **k: _FakeResponse(PROXY_LIST)
    )
    monkeypatch.setattr(
        accounts, "ping_proxy", lambda proxy: "1.1.1.1" in proxy["http"]
    )

    proxies = accounts.fetch_proxies("https://example.com/proxies", validate=True)

    assert len(proxies) == 1
    assert "1.1.1.1" in proxies[0]["http"]


def test_fetch_proxies_no_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        accounts.requests, "get", lambda *a, **k: _FakeResponse(PROXY_LIST)
    )
    monkeypatch.setattr(accounts, "ping_proxy", lambda proxy: False)

    with pytest.raises(ProxyConnectionError):
        accounts.fetch_proxies("https://example.com/proxies", validate=True)
