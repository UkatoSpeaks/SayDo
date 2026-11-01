"""Deterministic verdicts (pure code, template explanations)."""

from decimal import Decimal

import pytest

from app.models import ClaimType, Direction, VerificationStatus
from app.services.derived_metrics import ActualsIndex
from app.services.deterministic_check import (
    Outcome,
    TargetKind,
    ThreadView,
    baseline_period,
    deterministic_check,
    target_bounds,
)
from app.services.linking import Mention
from tests.verification_factories import actuals, mention, ordinal, thread

S = VerificationStatus
D = Decimal
AS_OF = ordinal("Q4FY26")
FY26 = (2026, None)


def margin(low: str, high: str | None = None, call: str = "Q1FY26", quote: str = "x") -> Mention:
    return mention(
        call, metric="ebitda_margin", low=low, high=high, unit="percent", target=FY26, quote=quote
    )


MARGIN_FY26 = actuals(("ebitda_margin", 2026, None, "18.5", "percent"))


def check(
    t: ThreadView, index: ActualsIndex = MARGIN_FY26, *, tolerance_pct: float = 0.0
) -> Outcome:
    return deterministic_check(t, index, AS_OF, tolerance_pct=tolerance_pct)


def kind(m: Mention) -> TargetKind:
    bounds = target_bounds(m)
    assert bounds is not None
    return bounds.kind


def test_in_range_is_delivered() -> None:
    result = check(thread(margin("18", "19")))
    assert result.status is S.DELIVERED
    assert result.reasoning == "Delivered: FY26 EBITDA margin was 18.5% vs target 18–19%."
    assert result.actual is not None and result.actual.actual_id is not None
    assert result.details["actual"]["value"] == "18.5"


def test_below_range_is_missed_with_gap() -> None:
    result = check(thread(margin("19", "20")))
    assert result.status is S.MISSED
    assert result.computed_gap == D("-0.5")
    assert result.gap_pct == D("-2.63")
    assert not result.near_miss
    assert "(-0.5%, -2.63%)" in result.reasoning


def test_above_range_beats_a_higher_is_better_target() -> None:
    result = check(thread(margin("17", "18")))
    assert result.status is S.DELIVERED
    assert "beating the target 17–18% by 0.5%" in result.reasoning
    assert result.computed_gap == D("0.5")


def test_above_range_misses_a_lower_is_better_target() -> None:
    debt = mention(metric="net_debt", low="200", high="250", unit="inr_crore", target=FY26)
    index = actuals(("net_debt", 2026, None, "260", "inr_crore"))
    result = check(thread(debt), index)
    assert result.status is S.MISSED
    assert result.computed_gap == D(10)
    assert result.gap_pct == D("4.00")


def test_neutral_metric_outside_range_either_way_is_missed() -> None:
    capex = mention(metric="capex", low="300", high="350", unit="inr_crore", target=FY26)
    index = actuals(("capex", 2026, None, "400", "inr_crore"))
    assert check(thread(capex), index).status is S.MISSED


def test_single_sided_at_least() -> None:
    claim = margin("25", quote="We will deliver at least 25% EBITDA margin in FY26.")
    assert kind(claim) is TargetKind.AT_LEAST
    high = actuals(("ebitda_margin", 2026, None, "31", "percent"))
    low = actuals(("ebitda_margin", 2026, None, "24", "percent"))
    assert check(thread(claim), high).status is S.DELIVERED
    missed = check(thread(claim), low)
    assert missed.status is S.MISSED
    assert "vs target at least 25%" in missed.reasoning


def test_single_sided_at_most() -> None:
    claim = mention(
        metric="capex",
        low="300",
        unit="inr_crore",
        target=FY26,
        quote="Capex will be up to Rs. 300 crore in FY26.",
    )
    assert kind(claim) is TargetKind.AT_MOST
    assert (
        check(thread(claim), actuals(("capex", 2026, None, "250", "inr_crore"))).status
        is S.DELIVERED
    )
    assert (
        check(thread(claim), actuals(("capex", 2026, None, "320", "inr_crore"))).status is S.MISSED
    )


def test_point_target_uses_polarity() -> None:
    # "20% margin" is a floor; "net debt of 200 crore" a ceiling.
    assert kind(margin("20")) is TargetKind.AT_LEAST
    debt = mention(metric="net_debt", low="200", unit="inr_crore")
    assert kind(debt) is TargetKind.AT_MOST
    over_the_period = margin("20", quote="We will hold 20% margin over the next two years.")
    assert kind(over_the_period) is TargetKind.AT_LEAST


def test_lowered_then_met_keeps_original_target_visible() -> None:
    first = mention("Q1FY26", metric="pat", low="450", unit="inr_crore", target=FY26)
    lowered = mention("Q3FY26", metric="pat", low="400", unit="inr_crore", target=FY26)
    index = actuals(("pat", 2026, None, "410", "inr_crore"))
    result = check(thread(first, lowered), index)
    assert result.status is S.DELIVERED
    assert result.reasoning.startswith(
        "Delivered (target lowered from at least ₹450 crore to at least ₹400 crore)"
    )
    assert result.details["original_target"]["target_low"] == "450"
    assert result.details["target_used"]["target_low"] == "400"


def test_near_miss_within_tolerance() -> None:
    result = check(thread(margin("19", "20")), tolerance_pct=3.0)
    assert result.status is S.MISSED
    assert result.near_miss
    assert "near miss" in result.reasoning
    assert not check(thread(margin("19", "20")), tolerance_pct=2.0).near_miss


def test_unit_conversion_and_mismatch() -> None:
    capex = mention(metric="capex", low="300", high="350", unit="inr_crore", target=FY26)
    lakh = actuals(("capex", 2026, None, "32000", "inr_lakh"))
    result = check(thread(capex), lakh)
    assert result.status is S.DELIVERED
    assert "₹320 crore (entered as ₹32,000 lakh)" in result.reasoning

    usd = actuals(("capex", 2026, None, "40", "usd_mn"))
    result = check(thread(capex), usd)
    assert result.status is S.UNVERIFIABLE
    assert "cannot be converted" in result.reasoning


def test_bps_target_against_percent_actual_is_a_unit_mismatch() -> None:
    # A target in basis points is a change; a margin actual is a level. Never compared.
    claim = mention(metric="ebitda_margin", low="1850", high="1900", unit="bps", target=FY26)
    result = check(thread(claim))
    assert result.status is S.UNVERIFIABLE
    assert result.reasoning.startswith("Unverifiable (unit mismatch)")


def test_hcltech_q4_charge_is_not_a_miss() -> None:
    # Regression: "I expect a similar kind of charge in the Q4 also" (50 bps) was
    # compared with the 16.54% Q4FY26 margin and called missed by 3208%.
    charge = mention(
        "Q3FY26",
        metric="ebit_margin",
        low="50",
        unit="bps",
        direction=Direction.DECREASE,
        target=(2026, 4),
        quote="I expect a similar kind of charge in the Q4 also.",
    )
    result = check(thread(charge), actuals(("ebit_margin", 2026, 4, "16.54", "percent")))
    assert result.status is S.UNVERIFIABLE
    assert "unit mismatch" in result.reasoning


def test_absurd_gap_is_unverifiable_not_missed() -> None:
    # A miss by more than 200% of the target means a unit went wrong somewhere.
    ceiling = mention(
        metric="ebitda_margin", low="0.5", unit="percent", target=FY26, direction=Direction.DECREASE
    )
    result = check(thread(ceiling), actuals(("ebitda_margin", 2026, None, "17.21", "percent")))
    assert result.status is S.UNVERIFIABLE
    assert result.reasoning.startswith("Unverifiable (unit mismatch)")
    assert "more than 200%" in result.reasoning
    # A plain miss stays a miss.
    floor = mention(metric="ebitda_margin", low="19", high="20", unit="percent", target=FY26)
    missed = check(thread(floor), actuals(("ebitda_margin", 2026, None, "17.21", "percent")))
    assert missed.status is S.MISSED


def test_period_not_over_is_in_progress() -> None:
    fy27 = mention(metric="ebitda_margin", low="19", high="20", unit="percent", target=(2027, None))
    result = check(thread(fy27))
    assert result.status is S.IN_PROGRESS
    assert result.reasoning == (
        "In progress: the target period FY27 ends after the latest data (Q4FY26)."
    )


def test_no_actual_is_unverifiable() -> None:
    result = check(thread(margin("18", "19")), actuals())
    assert result.status is S.UNVERIFIABLE
    assert "has not been entered yet" in result.reasoning


def test_derived_actual_is_used_and_explained() -> None:
    index = actuals(
        *[
            ("pat", 2026, q, v, "inr_crore")
            for q, v in zip((1, 2, 3, 4), ("95", "100", "105", "110"), strict=True)
        ]
    )
    claim = mention(metric="pat", low="400", unit="inr_crore", target=FY26)
    result = check(thread(claim), index)
    assert result.status is S.DELIVERED
    assert result.actual is not None and result.actual.derived and result.actual.actual_id is None
    assert "[derived: Sum of Q1–Q4 FY26 PAT" in result.reasoning


@pytest.mark.parametrize(
    ("direction", "fy26", "status"),
    [
        (Direction.INCREASE, "78", S.DELIVERED),
        (Direction.INCREASE, "73", S.MISSED),  # +1.4%: inside the ±2% band
        (Direction.DECREASE, "70", S.DELIVERED),
        (Direction.MAINTAIN, "73", S.DELIVERED),
        (Direction.MAINTAIN, "78", S.MISSED),
    ],
)
def test_directional(direction: Direction, fy26: str, status: VerificationStatus) -> None:
    claim = mention(
        "Q1FY26",
        metric="capacity_utilization",
        direction=direction,
        claim_type=ClaimType.DIRECTIONAL,
        target=FY26,
    )
    index = actuals(
        ("capacity_utilization", 2025, None, "72", "percent"),
        ("capacity_utilization", 2026, None, fy26, "percent"),
    )
    result = check(thread(claim), index)
    assert result.status is status
    assert result.baseline is not None and result.baseline.period_label == "FY25"
    assert "changes within ±2% count as no change" in result.reasoning


def test_directional_without_baseline_is_unverifiable() -> None:
    claim = mention(metric="capacity_utilization", direction=Direction.INCREASE, target=FY26)
    index = actuals(("capacity_utilization", 2026, None, "78", "percent"))
    result = check(thread(claim), index)
    assert result.status is S.UNVERIFIABLE and "baseline" in result.reasoning


def test_baseline_periods() -> None:
    # Annual target in a later year: the call's fiscal year.
    assert baseline_period(mention("Q2FY26", target=(2027, None))) == (2026, None)
    # Annual target in the call's own year: the year before.
    assert baseline_period(mention("Q2FY26", target=(2026, None))) == (2025, None)
    # Quarterly target after the call: the call's quarter.
    assert baseline_period(mention("Q2FY26", target=(2026, 4))) == (2026, 2)
    # Growth metrics: same period last year.
    assert baseline_period(mention("Q2FY26", metric="revenue_growth", target=(2027, None))) == (
        2026,
        None,
    )
