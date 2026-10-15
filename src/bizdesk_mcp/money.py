"""Money helpers.

All amounts are stored as integer *cents* so totals never drift because of
floating-point maths. Quantities are stored as integer *thousandths* so a
quantity like 2.5 hours or 0.125 kg is exact too.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Symbols that the built-in PDF fonts can draw. Anything else is shown as its ISO code.
_SYMBOLS = {"USD": "$", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$", "EUR": "€", "GBP": "£"}


class MoneyError(ValueError):
    """Raised when an amount, quantity or rate cannot be understood."""


def _to_decimal(value: object, what: str) -> Decimal:
    if isinstance(value, bool):  # bool is an int subclass; never a valid amount
        raise MoneyError(f"{what} must be a number, not true/false")
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise MoneyError(f"{what} '{value}' is not a valid number") from None
    if not d.is_finite():
        raise MoneyError(f"{what} must be a finite number")
    return d


def to_cents(value: object, *, what: str = "amount", allow_negative: bool = False) -> int:
    """Convert '1,250.50', 1250.5 or Decimal('1250.5') to 125050 cents."""
    d = _to_decimal(value, what)
    if d < 0 and not allow_negative:
        raise MoneyError(f"{what} cannot be negative")
    cents = d * 100
    if cents != cents.to_integral_value():
        raise MoneyError(f"{what} can have at most 2 decimal places")
    return int(cents)


def to_milli(value: object, *, what: str = "quantity") -> int:
    """Convert a quantity such as 2.5 to 2500 thousandths (max 3 decimals)."""
    d = _to_decimal(value, what)
    if d <= 0:
        raise MoneyError(f"{what} must be greater than zero")
    milli = d * 1000
    if milli != milli.to_integral_value():
        raise MoneyError(f"{what} can have at most 3 decimal places")
    return int(milli)


def rate_to_bp(value: object) -> int:
    """Convert a tax rate given as a percentage (e.g. 8.25) to basis points (825)."""
    d = _to_decimal(value, "tax rate")
    if d < 0 or d > 100:
        raise MoneyError("tax rate must be between 0 and 100 (percent)")
    bp = d * 100
    if bp != bp.to_integral_value():
        raise MoneyError("tax rate can have at most 2 decimal places")
    return int(bp)


def line_total_cents(quantity_milli: int, unit_price_cents: int) -> int:
    """quantity × unit price, rounded half-up to the cent."""
    exact = Decimal(quantity_milli) * Decimal(unit_price_cents) / Decimal(1000)
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def tax_cents(subtotal_cents: int, rate_bp: int) -> int:
    exact = Decimal(subtotal_cents) * Decimal(rate_bp) / Decimal(10000)
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_money(cents: int, currency: str = "USD") -> str:
    """125050, 'USD' -> '$1,250.50'; 125050, 'INR' -> 'INR 1,250.50'."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    body = f"{whole:,}.{frac:02d}"
    code = currency.upper()
    symbol = _SYMBOLS.get(code)
    return f"{sign}{symbol}{body}" if symbol else f"{sign}{code} {body}"


def format_quantity(quantity_milli: int) -> str:
    d = (Decimal(quantity_milli) / 1000).normalize()
    return f"{d:f}"


def format_rate(rate_bp: int) -> str:
    d = (Decimal(rate_bp) / 100).normalize()
    return f"{d:f}%"
