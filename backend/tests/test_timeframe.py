"""Deterministic timeframe resolution (services/timeframe.py)."""

import pytest

from app.services.timeframe import model_timeframe_is_consistent, resolve_timeframe

# (raw, call FY, call quarter, expected FY, expected quarter, is_range)
CASES: list[tuple[str, int, int | None, int | None, int | None, bool]] = [
    # Explicit fiscal years
    ("FY27", 2026, 2, 2027, None, False),
    ("by FY27", 2026, 2, 2027, None, False),
    ("FY2027", 2026, 2, 2027, None, False),
    ("FY 2026-27", 2026, 2, 2027, None, False),
    ("FY26-27", 2026, 2, 2027, None, False),
    ("FY'28", 2026, 2, 2028, None, False),
    ("fiscal 2027", 2026, 2, 2027, None, False),
    ("by the end of FY27", 2026, 2, 2027, None, False),
    # Quarter + fiscal year
    ("Q3FY26", 2026, 2, 2026, 3, False),
    ("Q3 FY26", 2026, 2, 2026, 3, False),
    ("Q4 of FY27", 2026, 2, 2027, 4, False),
    ("FY27 Q1", 2026, 2, 2027, 1, False),
    ("third quarter of FY27", 2026, 2, 2027, 3, False),
    # Halves
    ("H1 FY27", 2026, 2, 2027, 2, True),
    ("H2FY27", 2026, 2, 2027, 4, True),
    ("second half of FY27", 2026, 2, 2027, 4, True),
    ("in H2", 2026, 2, 2026, 4, True),
    ("H1 of next year", 2026, 2, 2027, 2, True),
    # Quarter of a relative year
    ("Q3 of this year", 2026, 2, 2026, 3, False),
    ("Q1 of next year", 2026, 2, 2027, 1, False),
    # The Q2FY26 rule: the call is held after Q2 ends (Oct-Nov 2025)
    ("this year", 2026, 2, 2026, None, False),
    ("current fiscal", 2026, 2, 2026, None, False),
    ("by the end of this financial year", 2026, 2, 2026, None, False),
    ("next year", 2026, 2, 2027, None, False),
    ("next fiscal", 2026, 2, 2027, None, False),
    ("next quarter", 2026, 2, 2026, 3, False),
    ("by year end", 2026, 2, 2026, None, False),
    ("by Q3", 2026, 2, 2026, 3, False),
    # Year boundaries: a Q4FY26 call is held in Apr-May 2026, i.e. in FY27
    ("next quarter", 2026, 4, 2027, 1, False),
    ("this year", 2026, 4, 2027, None, False),
    ("next year", 2026, 4, 2028, None, False),
    ("by Q3", 2026, 4, 2027, 3, False),
    ("Q1", 2026, 3, 2027, 1, False),
    ("next quarter", 2026, 3, 2026, 4, False),
    ("this year", 2026, 3, 2026, None, False),
    ("this year", 2026, None, 2027, None, False),  # annual document: like a Q4 call
    # Multi-period phrases -> last period + range flag
    ("next two years", 2026, 2, 2028, None, True),
    ("over the next 3 years", 2026, 2, 2029, None, True),
    ("next 2-3 years", 2026, 2, 2029, None, True),
    ("in the next couple of years", 2026, 2, 2028, None, True),
    ("next three quarters", 2026, 2, 2027, 1, True),
    ("over the next 18 months", 2026, 2, 2027, 4, True),
    ("next 1 year", 2026, 2, 2027, None, False),
    # Month + year
    ("by March 2027", 2026, 2, 2027, 4, False),
    ("by December 2026", 2026, 2, 2027, 3, False),
    ("April 2026", 2026, 2, 2027, 1, False),
    # Open-ended
    ("medium term", 2026, 2, None, None, False),
    ("over the long-term", 2026, 2, None, None, False),
    ("in the near term", 2026, 2, None, None, False),
]


@pytest.mark.parametrize(("raw", "call_fy", "call_q", "fy", "quarter", "is_range"), CASES)
def test_resolution_table(
    raw: str,
    call_fy: int,
    call_q: int | None,
    fy: int | None,
    quarter: int | None,
    is_range: bool,
) -> None:
    resolved = resolve_timeframe(raw, call_fy, call_q)
    assert resolved is not None, raw
    assert (resolved.fiscal_year, resolved.fiscal_quarter, resolved.is_range) == (
        fy,
        quarter,
        is_range,
    ), raw


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "by 2027", "post monsoon", "soon", "this quarter"]
)
def test_unresolvable_returns_none(raw: str | None) -> None:
    assert resolve_timeframe(raw, 2026, 2) is None


def test_model_values_kept_only_when_consistent() -> None:
    # "by 2027": calendar or fiscal, so FY27 or FY28 are both plausible.
    assert model_timeframe_is_consistent("by 2027", 2027, None, 2026, 2) == (2027, None)
    assert model_timeframe_is_consistent("by 2027", 2028, None, 2026, 2) == (2028, None)
    assert model_timeframe_is_consistent("by 2027", 2030, None, 2026, 2) == (None, None)
    # Before the call, or absurdly far out.
    assert model_timeframe_is_consistent("post monsoon", 2024, None, 2026, 2) == (None, None)
    assert model_timeframe_is_consistent("eventually", 2045, None, 2026, 2) == (None, None)
    # A quarter needs some quarter-ish word in the raw text.
    assert model_timeframe_is_consistent("post monsoon", 2026, 3, 2026, 2) == (2026, None)
    assert model_timeframe_is_consistent("this quarter", 2026, 3, 2026, 2) == (2026, 3)
    # Nothing said -> nothing kept.
    assert model_timeframe_is_consistent(None, 2027, 1, 2026, 2) == (None, None)
    # "18 months" is not a year number.
    assert model_timeframe_is_consistent("18 months or so", 2027, None, 2026, 2) == (2027, None)
