from base64 import b64encode

from steampy import guard
from steampy.confirmation import Tag

SHARED_SECRET = b64encode(b"1234567890abcdefghij")
IDENTITY_SECRET = b64encode(b"abcdefghijklmnoprstu")


def test_one_time_code() -> None:
    assert guard.generate_one_time_code(SHARED_SECRET, 1469184207) == "P2QJN"


def test_confirmation_key() -> None:
    key = guard.generate_confirmation_key(IDENTITY_SECRET, Tag.CONF.value, 1470838334)
    assert key == b"pWqjnkcwqni+t/n+5xXaEa0SGeA="


def test_generate_device_id() -> None:
    device_id = guard.generate_device_id("12341234123412345")
    assert device_id == "android:677cf5aa-3300-7807-d1e2-c408142742e2"


def test_load_steam_guard() -> None:
    guard_json_str = (
        '{"steamid": 12345678, "shared_secret": "SHARED_SECRET", '
        '"identity_secret": "IDENTITY_SECRET"}'
    )
    guard_data = guard.load_steam_guard(guard_json_str)
    for key in ("steamid", "shared_secret", "identity_secret"):
        assert key in guard_data
        assert isinstance(guard_data[key], str)
