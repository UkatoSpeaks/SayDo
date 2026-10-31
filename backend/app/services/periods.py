"""Quarter ordinals for comparing fiscal periods.

``ordinal = fiscal_year * 4 + quarter - 1``. A full year (quarter ``None``) is
compared by its last quarter, so FY27 ends at the same ordinal as Q4FY27.
"""

from app.services.fiscal import period_label


def end_ordinal(fiscal_year: int, fiscal_quarter: int | None) -> int:
    return fiscal_year * 4 + (fiscal_quarter or 4) - 1


def start_ordinal(fiscal_year: int, fiscal_quarter: int | None) -> int:
    return fiscal_year * 4 + (fiscal_quarter or 1) - 1


def from_ordinal(ordinal: int) -> tuple[int, int]:
    return ordinal // 4, ordinal % 4 + 1


def ordinal_label(ordinal: int) -> str:
    return period_label(*from_ordinal(ordinal))


def label_or_none(fiscal_year: int | None, fiscal_quarter: int | None) -> str | None:
    return None if fiscal_year is None else period_label(fiscal_year, fiscal_quarter)


def previous_year(fiscal_year: int, fiscal_quarter: int | None) -> tuple[int, int | None]:
    """The same period one year earlier (Q2FY26 -> Q2FY25, FY26 -> FY25)."""
    return fiscal_year - 1, fiscal_quarter
