"""Indian fiscal-year helpers.

The Indian financial year runs 1 April to 31 March and is named after the
calendar year in which it ends: FY27 = 1 Apr 2026 to 31 Mar 2027.

    Q1 = Apr-Jun, Q2 = Jul-Sep, Q3 = Oct-Dec (all in the starting calendar year)
    Q4 = Jan-Mar (in the ending calendar year)
"""

import calendar
import re
from datetime import date
from typing import NamedTuple

MIN_FISCAL_YEAR = 2000
MAX_FISCAL_YEAR = 2099

# Accepts e.g. "Q2FY26", "Q2 FY26", "q2fy2026", "FY26", "Q2-FY26", "FY'26",
# "FY2025-26", "FY25-26", "Q3 FY 2025-26".
_PERIOD_RE = re.compile(
    r"""
    ^\s*
    (?:Q(?P<quarter>[1-4])[\s\-_]*)?
    FY[\s\-_']*
    (?P<year>\d{4}|\d{2})
    (?:\s*[-/]\s*(?P<end>\d{4}|\d{2}))?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


class FiscalPeriod(NamedTuple):
    fiscal_year: int
    fiscal_quarter: int | None


def _check_year(fiscal_year: int) -> None:
    if not MIN_FISCAL_YEAR <= fiscal_year <= MAX_FISCAL_YEAR:
        raise ValueError(
            f"fiscal_year must be between {MIN_FISCAL_YEAR} and {MAX_FISCAL_YEAR}, "
            f"got {fiscal_year}"
        )


def _check_quarter(fiscal_quarter: int | None) -> None:
    if fiscal_quarter is not None and not 1 <= fiscal_quarter <= 4:
        raise ValueError(f"fiscal_quarter must be 1-4, got {fiscal_quarter}")


def _expand_year(raw: str) -> int:
    return 2000 + int(raw) if len(raw) == 2 else int(raw)


def date_to_fiscal(d: date) -> FiscalPeriod:
    """Return the (fiscal_year, fiscal_quarter) a calendar date falls in."""
    if d.month >= 4:
        return FiscalPeriod(d.year + 1, (d.month - 4) // 3 + 1)
    return FiscalPeriod(d.year, 4)


def period_label(fiscal_year: int, fiscal_quarter: int | None = None) -> str:
    """Format a period as ``"Q2FY26"``, or ``"FY26"`` when there is no quarter."""
    _check_year(fiscal_year)
    _check_quarter(fiscal_quarter)
    fy = f"FY{fiscal_year % 100:02d}"
    return fy if fiscal_quarter is None else f"Q{fiscal_quarter}{fy}"


def parse_period_label(label: str) -> FiscalPeriod:
    """Parse a period label such as ``"Q2FY26"`` or ``"FY26"``.

    Raises ``ValueError`` if the label is not recognised.
    """
    match = _PERIOD_RE.match(label)
    if match is None:
        raise ValueError(f"Unrecognised period label: {label!r}")

    year = _expand_year(match["year"])
    if match["end"] is not None:
        # Range form "FY2025-26": the fiscal year is named after the end year.
        end = match["end"]
        consecutive = int(end) == (year + 1) % 100 if len(end) == 2 else int(end) == year + 1
        if not consecutive:
            raise ValueError(f"Fiscal year range must span consecutive years: {label!r}")
        year += 1

    _check_year(year)
    quarter = int(match["quarter"]) if match["quarter"] else None
    return FiscalPeriod(year, quarter)


def fiscal_quarter_date_range(
    fiscal_year: int, fiscal_quarter: int | None = None
) -> tuple[date, date]:
    """Return the inclusive (start, end) dates of a fiscal quarter or full fiscal year."""
    _check_year(fiscal_year)
    _check_quarter(fiscal_quarter)
    if fiscal_quarter is None:
        return date(fiscal_year - 1, 4, 1), date(fiscal_year, 3, 31)

    if fiscal_quarter == 4:
        year, start_month = fiscal_year, 1
    else:
        year, start_month = fiscal_year - 1, 4 + (fiscal_quarter - 1) * 3
    end_month = start_month + 2
    return date(year, start_month, 1), date(
        year, end_month, calendar.monthrange(year, end_month)[1]
    )
