"""Unit conversion for comparing targets with actuals.

Values are stored in the unit they were reported in and converted only when two
of them are compared. Conversions that need outside data (USD -> INR) or make no
sense (tonnes -> crore) return ``None``; callers then treat the comparison as
impossible and say why.

    1 crore = 100 lakh        1 bn = 100 crore        100 bps = 1 percent
"""

from decimal import Decimal

from app.models import ClaimUnit

# Units in one family convert into each other through a common base.
_INR_IN_CRORE = {
    ClaimUnit.INR_CRORE: Decimal(1),
    ClaimUnit.INR_LAKH: Decimal("0.01"),
    ClaimUnit.INR_BN: Decimal(100),
}
_RATE_IN_PERCENT = {
    ClaimUnit.PERCENT: Decimal(1),
    ClaimUnit.BPS: Decimal("0.01"),
}
_FAMILIES = (_INR_IN_CRORE, _RATE_IN_PERCENT)
INR_UNITS = frozenset(_INR_IN_CRORE)

UNIT_LABELS = {
    ClaimUnit.PERCENT: "%",
    ClaimUnit.INR_CRORE: "crore",
    ClaimUnit.INR_LAKH: "lakh",
    ClaimUnit.INR_BN: "bn",
    ClaimUnit.USD_MN: "USD mn",
    ClaimUnit.UNITS: "units",
    ClaimUnit.MW: "MW",
    ClaimUnit.TONNES: "tonnes",
    ClaimUnit.BPS: "bps",
    ClaimUnit.X: "x",
    ClaimUnit.COUNT: "",
}


def parse_unit(raw: str | None) -> ClaimUnit | None:
    if raw is None:
        return None
    try:
        return ClaimUnit(raw)
    except ValueError:
        return None


def can_convert(from_unit: str | None, to_unit: str | None) -> bool:
    source, target = parse_unit(from_unit), parse_unit(to_unit)
    if source is None or target is None:
        return False
    return source == target or any(source in f and target in f for f in _FAMILIES)


def convert(value: Decimal, from_unit: str | None, to_unit: str | None) -> Decimal | None:
    """``value`` expressed in ``to_unit``, or ``None`` when no conversion exists."""
    source, target = parse_unit(from_unit), parse_unit(to_unit)
    if source is None or target is None:
        return None
    if source == target:
        return value
    for family in _FAMILIES:
        if source in family and target in family:
            return value * family[source] / family[target]
    return None


def conversion_error(from_unit: str | None, to_unit: str | None) -> str:
    return f"cannot compare {from_unit or 'no unit'} with {to_unit or 'no unit'}"


EN_DASH = "\u2013"


def plain(value: Decimal) -> str:
    """Decimal without exponent or trailing zeros: 4E+4 -> "40000", 18.50 -> "18.5"."""
    return format(value.normalize(), "f")


def format_range(low: Decimal, high: Decimal, unit: str | None) -> str:
    """ "18-19%", "₹300-350 crore", "40,000-50,000 tonnes" (with an en dash)."""
    low_text, high_text = format_value(low, unit), format_value(high, unit)
    if low == high:
        return low_text
    number_low = format_value(low, None)
    number_high = format_value(high, None)
    prefix, _, suffix = high_text.partition(number_high)
    if low_text == f"{prefix}{number_low}{suffix}":
        return f"{prefix}{number_low}{EN_DASH}{number_high}{suffix}"
    return f"{low_text}{EN_DASH}{high_text}"


def format_value(value: Decimal, unit: str | None) -> str:
    """Human-readable value for explanations: "18.5%", "₹4,620 crore", "40,000 tonnes"."""
    rounded = value.quantize(Decimal("0.01")) if value != value.to_integral_value() else value
    number = f"{rounded.normalize():,f}"
    parsed = parse_unit(unit)
    if parsed is ClaimUnit.PERCENT:
        return f"{number}%"
    if parsed in (ClaimUnit.INR_CRORE, ClaimUnit.INR_LAKH, ClaimUnit.INR_BN):
        return f"₹{number} {UNIT_LABELS[parsed]}"
    if parsed is ClaimUnit.USD_MN:
        return f"USD {number} mn"
    if parsed is ClaimUnit.X:
        return f"{number}x"
    if parsed is None or parsed is ClaimUnit.COUNT:
        return number
    return f"{number} {UNIT_LABELS[parsed]}"
