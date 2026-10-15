import pytest

from bizdesk_mcp.money import (
    MoneyError,
    format_money,
    format_quantity,
    format_rate,
    line_total_cents,
    rate_to_bp,
    tax_cents,
    to_cents,
    to_milli,
)


@pytest.mark.parametrize(
    "value, cents",
    [("1,250.50", 125050), (1250.5, 125050), ("0.1", 10), (0.29, 29), ("19.99", 1999), (7, 700), ("  45 ", 4500)],
)
def test_to_cents(value, cents):
    assert to_cents(value) == cents


@pytest.mark.parametrize("bad", ["abc", "1.005", "-5", True, "inf", "NaN", ""])
def test_to_cents_rejects(bad):
    with pytest.raises(MoneyError):
        to_cents(bad)


def test_to_milli_and_rates():
    assert to_milli("2.5") == 2500
    assert to_milli(0.125) == 125
    with pytest.raises(MoneyError):
        to_milli(0)
    with pytest.raises(MoneyError):
        to_milli("1.0001")
    assert rate_to_bp("8.25") == 825
    with pytest.raises(MoneyError):
        rate_to_bp(101)


def test_rounding_is_half_up_and_exact():
    # 3 x $0.335 would drift with floats; here it is exact then rounded once.
    assert line_total_cents(3000, 34) == 102
    assert line_total_cents(2500, 1999) == 4998  # 49.975 -> 49.98
    assert tax_cents(33016, 0) == 0
    assert tax_cents(30500, 825) == 2516  # 25.1625 -> 25.16
    assert tax_cents(200, 825) == 17  # 16.5 -> 17 (half up)


def test_formatting():
    assert format_money(125050, "USD") == "$1,250.50"
    assert format_money(-500, "GBP") == "-£5.00"
    assert format_money(9900, "INR") == "INR 99.00"
    assert format_quantity(2500) == "2.5"
    assert format_quantity(12000) == "12"
    assert format_rate(825) == "8.25%"
