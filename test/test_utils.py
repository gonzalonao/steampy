from decimal import Decimal

import pytest

from steampy import utils


def test_text_between() -> None:
    assert utils.text_between('var a = "value";', 'var a = "', '";') == "value"


def test_texts_between() -> None:
    text = "<li>element 1</li>\n<li>some random element</li>"
    assert list(utils.texts_between(text, "<li>", "</li>")) == [
        "element 1",
        "some random element",
    ]


def test_account_id_to_steam_id() -> None:
    assert utils.account_id_to_steam_id("358617487") == "76561198318883215"


def test_steam_id_to_account_id() -> None:
    assert utils.steam_id_to_account_id("76561198318883215") == "358617487"


def test_get_key_value_from_url() -> None:
    url = "https://steamcommunity.com/tradeoffer/new/?partner=aaa&token=bbb"
    assert utils.get_key_value_from_url(url, "partner") == "aaa"
    assert utils.get_key_value_from_url(url, "token") == "bbb"


def test_get_key_value_from_url_case_insensitive() -> None:
    url = "https://steamcommunity.com/tradeoffer/new/?Partner=aaa&Token=bbb"
    assert utils.get_key_value_from_url(url, "partner", case_sensitive=False) == "aaa"
    assert utils.get_key_value_from_url(url, "token", case_sensitive=False) == "bbb"


def test_calculate_gross_price() -> None:
    steam_fee = Decimal("0.05")
    publisher_fee = Decimal("0.1")
    assert utils.calculate_gross_price(
        Decimal("0.01"), publisher_fee, steam_fee
    ) == Decimal("0.03")
    assert utils.calculate_gross_price(
        Decimal("0.10"), publisher_fee, steam_fee
    ) == Decimal("0.12")
    assert utils.calculate_gross_price(
        Decimal(100), publisher_fee, steam_fee
    ) == Decimal(115)


def test_calculate_net_price() -> None:
    steam_fee = Decimal("0.05")
    publisher_fee = Decimal("0.1")
    assert utils.calculate_net_price(
        Decimal("0.03"), publisher_fee, steam_fee
    ) == Decimal("0.01")
    assert utils.calculate_net_price(
        Decimal("0.12"), publisher_fee, steam_fee
    ) == Decimal("0.10")
    assert utils.calculate_net_price(Decimal(115), publisher_fee, steam_fee) == Decimal(
        100
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.234,56 €", "1234.56"),  # european grouping + decimal comma
        ("1,234.56", "1234.56"),  # us grouping + decimal dot
        ("1,23", "1.23"),  # decimal comma only
        ("€2.50", "2.50"),  # leading currency symbol
        ("-", "0"),  # empty price placeholder
        ("", "0"),  # nothing at all
    ],
)
def test_clean_price(raw: str, expected: str) -> None:
    assert utils.clean_price(raw) == expected
