"""Unit conversion and derived metrics (pure code)."""

from decimal import Decimal

import pytest

from app.services.derived_metrics import ActualsIndex
from app.services.units import can_convert, convert, format_range, format_value
from tests.verification_factories import actuals

D = Decimal


@pytest.mark.parametrize(
    ("value", "source", "target", "expected"),
    [
        ("32000", "inr_lakh", "inr_crore", "320"),
        ("320", "inr_crore", "inr_lakh", "32000"),
        ("1.5", "inr_bn", "inr_crore", "150"),
        ("150", "inr_crore", "inr_bn", "1.5"),
        ("250", "bps", "percent", "2.5"),
        ("2.5", "percent", "bps", "250"),
        ("7", "percent", "percent", "7"),
    ],
)
def test_convert(value: str, source: str, target: str, expected: str) -> None:
    result = convert(D(value), source, target)
    assert result is not None
    assert result == D(expected)


@pytest.mark.parametrize(
    ("source", "target"),
    [("usd_mn", "inr_crore"), ("tonnes", "inr_crore"), ("percent", "inr_crore"), (None, "x")],
)
def test_impossible_conversions_return_none(source: str | None, target: str) -> None:
    assert convert(D(1), source, target) is None
    assert not can_convert(source, target)


def test_formatting() -> None:
    assert format_value(D("4620"), "inr_crore") == "₹4,620 crore"
    assert format_value(D("18.5277"), "percent") == "18.53%"
    assert format_range(D(300), D(350), "inr_crore") == "₹300–350 crore"
    assert format_range(D(18), D(19), "percent") == "18–19%"


QUARTERS = [
    ("revenue", 2026, 1, "1100", "inr_crore"),
    ("revenue", 2026, 2, "1150", "inr_crore"),
    ("revenue", 2026, 3, "1170", "inr_crore"),
    ("revenue", 2026, 4, "1200", "inr_crore"),
    ("ebitda", 2026, 1, "198", "inr_crore"),
    ("ebitda", 2026, 2, "212", "inr_crore"),
    ("ebitda", 2026, 3, "218", "inr_crore"),
    ("ebitda", 2026, 4, "228", "inr_crore"),
]


def test_annual_flow_is_sum_of_four_quarters() -> None:
    index = actuals(*QUARTERS)
    revenue = index.resolve("revenue", 2026, None)
    assert revenue is not None
    assert revenue.value == D(4620)
    assert revenue.derived
    assert revenue.derived_from is not None and "Sum of Q1–Q4 FY26 Revenue" in revenue.derived_from
    assert len(revenue.source_actual_ids) == 4


def test_annual_sum_converts_units_to_the_first_quarter() -> None:
    index = actuals(
        ("capex", 2026, 1, "100", "inr_crore"),
        ("capex", 2026, 2, "5000", "inr_lakh"),
        ("capex", 2026, 3, "1", "inr_bn"),
        ("capex", 2026, 4, "50", "inr_crore"),
    )
    capex = index.resolve("capex", 2026, None)
    assert capex is not None and capex.value == D(300) and capex.unit == "inr_crore"


def test_missing_quarter_means_no_derived_annual_value() -> None:
    index = actuals(*QUARTERS[:3], *QUARTERS[4:])  # revenue Q4 missing
    assert index.resolve("revenue", 2026, None) is None
    assert index.resolve("ebitda_margin", 2026, None) is None  # needs annual revenue too


def test_annual_margin_is_recomputed_from_sums_not_averaged() -> None:
    index = actuals(*QUARTERS)
    margin = index.resolve("ebitda_margin", 2026, None)
    assert margin is not None
    expected = D(856) / D(4620) * 100
    assert margin.value == expected
    # The average of the four quarterly margins is different.
    quarterly = [
        D(e) / D(r) * 100
        for (r, e) in zip(
            ("1100", "1150", "1170", "1200"), ("198", "212", "218", "228"), strict=True
        )
    ]
    assert margin.value != sum(quarterly) / 4
    assert margin.unit == "percent"
    assert margin.derived_from is not None
    assert "EBITDA ₹856 crore / Revenue ₹4,620 crore" in margin.derived_from


def test_quarterly_margin_from_same_quarter_parts() -> None:
    margin = actuals(*QUARTERS).resolve("ebitda_margin", 2026, 1)
    assert margin is not None and margin.value == D(18)


def test_reported_value_wins_over_derivation() -> None:
    index = actuals(*QUARTERS, ("revenue", 2026, None, "4700", "inr_crore"))
    revenue = index.resolve("revenue", 2026, None)
    assert revenue is not None and not revenue.derived and revenue.value == D(4700)
    # A derived margin uses the reported annual revenue.
    margin = index.resolve("ebitda_margin", 2026, None)
    assert margin is not None and margin.value == D(856) / D(4700) * 100


def test_point_in_time_annual_is_q4_value() -> None:
    index = actuals(
        ("net_debt", 2026, 3, "300", "inr_crore"), ("net_debt", 2026, 4, "260", "inr_crore")
    )
    value = index.resolve("net_debt", 2026, None)
    assert value is not None and value.value == D(260) and value.derived
    assert value.derived_from is not None and "point-in-time" in value.derived_from
    assert (
        actuals(("net_debt", 2026, 3, "300", "inr_crore")).resolve("net_debt", 2026, None) is None
    )


def test_revenue_growth_is_yoy_from_base_metric() -> None:
    index = actuals(*QUARTERS, ("revenue", 2025, None, "4100", "inr_crore"))
    growth = index.resolve("revenue_growth", 2026, None)
    assert growth is not None
    assert growth.value == (D(4620) / D(4100) - 1) * 100
    assert growth.derived_from is not None and "Year-on-year" in growth.derived_from
    # Quarterly YoY needs the same quarter last year.
    assert index.resolve("revenue_growth", 2026, 1) is None


def test_volume_growth_and_gross_margin_are_never_derived() -> None:
    index = actuals(*QUARTERS)
    assert index.resolve("volume_growth", 2026, None) is None
    assert index.resolve("gross_margin", 2026, None) is None


def test_derived_values_lists_only_unreported() -> None:
    index = actuals(*QUARTERS, ("revenue", 2026, None, "4620", "inr_crore"))
    derived = {(v.metric, v.fiscal_year, v.fiscal_quarter) for v in index.derived_values()}
    assert ("revenue", 2026, None) not in derived  # reported
    assert ("ebitda", 2026, None) in derived
    assert ("ebitda_margin", 2026, None) in derived
    assert ("ebitda_margin", 2026, 2) in derived
    assert all(v.derived and v.derived_from for v in index.derived_values())


def test_empty_index() -> None:
    assert ActualsIndex([]).derived_values() == []
