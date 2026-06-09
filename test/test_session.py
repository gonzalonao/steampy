import pytest
from requests.exceptions import ProxyError

from steampy.exceptions import ProxyConnectionError
from steampy.session import RotatingProxySession

PROXIES = [
    {"http": "http://p1", "https": "http://p1"},
    {"http": "http://p2", "https": "http://p2"},
    {"http": "http://p3", "https": "http://p3"},
]


class _FakeResponse:
    status_code = 200


def _record_requests(session: RotatingProxySession, fail_times: int = 0) -> list:
    """Replace ``request`` with a recorder that can fail a few times first."""
    used_proxies: list = []

    def fake_request(method: str, url: str, **kwargs: object) -> _FakeResponse:
        used_proxies.append(kwargs.get("proxies"))
        if len(used_proxies) <= fail_times:
            raise ProxyError("boom")
        return _FakeResponse()

    session.request = fake_request  # type: ignore[method-assign]
    return used_proxies


def test_no_pool_behaves_like_plain_session() -> None:
    session = RotatingProxySession()
    used = _record_requests(session)
    session.rotating_get("https://example.com")
    assert used == [None]  # no proxy applied


def test_round_robin_rotation() -> None:
    session = RotatingProxySession()
    session.set_proxies_list(PROXIES, skip_ping=True)
    session._index = 0  # make the starting offset deterministic
    used = _record_requests(session)

    for _ in range(4):
        session.rotating_get("https://example.com")

    assert used == [PROXIES[0], PROXIES[1], PROXIES[2], PROXIES[0]]


def test_failover_to_next_proxy() -> None:
    session = RotatingProxySession()
    session.set_proxies_list(PROXIES, skip_ping=True)
    session._index = 0
    used = _record_requests(session, fail_times=1)

    session.rotating_get("https://example.com")

    # First proxy raised, so the second proxy served the request.
    assert used == [PROXIES[0], PROXIES[1]]


def test_failover_exhausted_raises() -> None:
    session = RotatingProxySession()
    session.set_proxies_list(PROXIES, skip_ping=True)
    _record_requests(session, fail_times=len(PROXIES))

    with pytest.raises(ProxyError, match="proxy attempts failed"):
        session.rotating_get("https://example.com")


def test_empty_pool_rejected() -> None:
    session = RotatingProxySession()
    with pytest.raises(ProxyConnectionError):
        session.set_proxies_list([], skip_ping=True)
