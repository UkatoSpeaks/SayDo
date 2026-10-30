"""Claim schema and the code-side checks on LLM output (no network, no DB)."""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.models import ClaimType, ClaimUnit, Direction, MetricNormalized
from app.services.claim_schema import ExtractedClaim, ExtractionResult, llm_json_schema
from app.services.claim_validation import (
    DedupeKey,
    find_quote,
    is_duplicate,
    numbers_in,
    validate_claim,
    validate_claims,
)

CHUNK = (
    "Thanks, Priya. We are guiding for an EBITDA margin of 19% to 20% for FY27. About "
    "two–thirds of the improvement this quarter came from operating leverage and mix, "
    "which we believe is “sustainable”, and the balance from lower commodity prices.\n"
    "We plan to add 1,200 stores by FY28 and capex will be about Rs.4,800 crore. Employee "
    "cost should come down by 50 bps, and payout will be at least 25 per cent."
)


def claim(**overrides: Any) -> ExtractedClaim:
    data: dict[str, Any] = {
        "quote": "We are guiding for an EBITDA margin of 19% to 20% for FY27.",
        "metric_raw": "EBITDA margin",
        "metric_normalized": "ebitda_margin",
        "claim_type": "numeric",
        "target_low": 19,
        "target_high": 20,
        "unit": "percent",
        "direction": "increase",
        "timeframe_raw": "for FY27",
        "target_fiscal_year": 2027,
        "target_fiscal_quarter": None,
        "confidence": 0.9,
    }
    data.update(overrides)
    return ExtractedClaim.model_validate(data)


# ------------------------------------------------------------------- schema


def test_schema_normalises_targets_and_years() -> None:
    single = claim(target_low=None, target_high=25)
    assert (single.target_low, single.target_high) == (25, 25)
    swapped = claim(target_low=20, target_high=19)
    assert (swapped.target_low, swapped.target_high) == (19, 20)
    assert claim(target_fiscal_year=27).target_fiscal_year == 2027
    assert claim(timeframe_raw="  ").timeframe_raw is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"metric_normalized": "headcount"},
        {"claim_type": "promise"},
        {"unit": "furlongs"},
        {"direction": "up"},
        {"target_fiscal_quarter": 5},
        {"target_fiscal_year": 1999},
        {"confidence": 1.5},
        {"quote": ""},
        {"surprise": "extra field"},
    ],
)
def test_schema_rejects_bad_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        claim(**overrides)


def test_result_requires_claims_list() -> None:
    assert ExtractionResult.model_validate_json('{"claims": []}').claims == []
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate_json('{"items": []}')
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate_json("not json")


def test_llm_json_schema_is_strict() -> None:
    schema = llm_json_schema(ExtractionResult)
    text = str(schema)
    assert "$ref" not in text and "$defs" not in text and "title" not in schema
    assert schema["required"] == ["claims"] and schema["additionalProperties"] is False
    item = schema["properties"]["claims"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(ExtractedClaim.model_fields)
    assert item["properties"]["target_low"]["type"] == ["number", "null"]
    unit = item["properties"]["unit"]
    assert unit["type"] == ["string", "null"] and None in unit["enum"] and "percent" in unit["enum"]
    assert "minimum" not in str(item["properties"]["confidence"])


# -------------------------------------------------------------------- quote


def test_quote_exact_and_normalised() -> None:
    match = find_quote("We are guiding for an EBITDA margin of 19% to 20% for FY27.", CHUNK)
    assert match is not None and match.ratio == 1.0
    # Straight quotes / hyphens / extra spaces vs the source's curly quotes and en dash.
    match = find_quote(
        "About two-thirds of the improvement   this quarter came from operating leverage and "
        'mix, which we believe is "sustainable"',
        CHUNK,
    )
    assert match is not None and match.ratio == 1.0
    assert match.source_span.startswith("About two–thirds")  # stored verbatim


def test_quote_fuzzy_match() -> None:
    match = find_quote("We are guiding for EBITDA margin of 19% to 20% in FY27.", CHUNK)
    assert match is not None and 0.9 <= match.ratio < 1.0
    assert match.source_span == "We are guiding for an EBITDA margin of 19% to 20% for FY27."


@pytest.mark.parametrize(
    "quote",
    [
        "We are guiding for an EBITDA margin of 22% to 24% for FY29.",
        "We expect revenue to double by FY27.",
        "What is the EBITDA margin guidance for FY27?",
    ],
)
def test_hallucinated_quote_rejected(quote: str) -> None:
    assert find_quote(quote, CHUNK) is None
    outcome = validate_claims([claim(quote=quote)], CHUNK, 2026, 2)
    assert outcome.accepted == [] and outcome.rejected_hallucinated_quote == 1


# ------------------------------------------------------------------ numbers


def test_numbers_in_handles_formatting_variants() -> None:
    found = numbers_in(
        "18 per cent, 1,200 stores, 1.2k units, Rs.4,800 crore, 1,25,000 tonnes, 2x, double, "
        "15-18%, FY27"
    )
    assert {18, 1200, 1.2, 4800, 4.8e10, 125000, 2, 15} <= found
    assert 27 not in found  # part of "FY27"


@pytest.mark.parametrize(
    ("quote", "low", "high", "unit"),
    [
        ("payout will be at least 25 per cent.", 25, 25, "percent"),
        ("We plan to add 1,200 stores by FY28", 1200, 1200, "count"),
        ("capex will be about Rs.4,800 crore.", 4800, 4800, "inr_crore"),
        ("Employee cost should come down by 50 bps", 50, 50, "bps"),
    ],
)
def test_numbers_present_in_source_are_kept(quote: str, low: float, high: float, unit: str) -> None:
    validated = validate_claim(
        claim(quote=quote, target_low=low, target_high=high, unit=unit, timeframe_raw=None),
        CHUNK,
        2026,
        2,
    )
    assert validated is not None
    assert validated.target_low == Decimal(str(low)) and validated.claim_type is ClaimType.NUMERIC


def test_number_with_k_suffix() -> None:
    text = "We will add about 1.2k stores next year."
    validated = validate_claim(
        claim(quote=text, target_low=1200, target_high=1200, unit="count"), text, 2026, 2
    )
    assert validated is not None and validated.target_low == Decimal(1200)
    assert str(validated.target_low) == "1200"  # not 1.2E+3


def test_numbers_not_in_source_are_nulled_and_downgraded() -> None:
    validated = validate_claim(claim(target_low=21, target_high=22), CHUNK, 2026, 2)
    assert validated is not None
    assert validated.target_low is None and validated.target_high is None
    assert validated.unit is None
    assert validated.claim_type is ClaimType.DIRECTIONAL
    assert validated.confidence == pytest.approx(0.9 * 0.6)
    assert any("numbers not found in source (21, 22)" in n for n in validated.notes)
    outcome = validate_claims([claim(target_low=21, target_high=22)], CHUNK, 2026, 2)
    assert outcome.numbers_downgraded == 1 and len(outcome.accepted) == 1


def test_claim_type_follows_targets() -> None:
    promoted = validate_claim(claim(claim_type="directional"), CHUNK, 2026, 2)
    assert promoted is not None and promoted.claim_type is ClaimType.NUMERIC
    demoted = validate_claim(claim(target_low=None, target_high=None), CHUNK, 2026, 2)
    assert demoted is not None and demoted.claim_type is ClaimType.DIRECTIONAL
    qualitative = validate_claim(
        claim(claim_type="qualitative", target_low=None, target_high=None), CHUNK, 2026, 2
    )
    assert qualitative is not None and qualitative.claim_type is ClaimType.QUALITATIVE


# ---------------------------------------------------------------- timeframe


def test_timeframe_resolved_in_code_overrides_model() -> None:
    validated = validate_claim(
        claim(timeframe_raw="next year", target_fiscal_year=2026), CHUNK, 2026, 2
    )
    assert validated is not None and validated.target_fiscal_year == 2027
    assert any("resolved in code" in n for n in validated.notes)

    ranged = validate_claim(claim(timeframe_raw="next two years"), CHUNK, 2026, 2)
    assert ranged is not None
    assert (ranged.target_fiscal_year, ranged.timeframe_is_range) == (2028, True)

    open_ended = validate_claim(claim(timeframe_raw="medium term"), CHUNK, 2026, 2)
    assert open_ended is not None and open_ended.target_fiscal_year is None


def test_model_timeframe_kept_only_if_consistent() -> None:
    kept = validate_claim(claim(timeframe_raw="by 2027", target_fiscal_year=2027), CHUNK, 2026, 2)
    assert kept is not None and kept.target_fiscal_year == 2027
    dropped = validate_claim(
        claim(timeframe_raw="by 2027", target_fiscal_year=2031), CHUNK, 2026, 2
    )
    assert dropped is not None and dropped.target_fiscal_year is None
    nothing_said = validate_claim(
        claim(timeframe_raw=None, target_fiscal_year=2027, target_fiscal_quarter=2), CHUNK, 2026, 2
    )
    assert nothing_said is not None
    assert (nothing_said.target_fiscal_year, nothing_said.target_fiscal_quarter) == (None, None)


# ------------------------------------------------------------------- dedupe


def key(**overrides: Any) -> DedupeKey:
    data: dict[str, Any] = {
        "metric_normalized": MetricNormalized.EBITDA_MARGIN.value,
        "target_fiscal_year": 2027,
        "target_fiscal_quarter": None,
        "target_low": Decimal(19),
        "target_high": Decimal(20),
        "unit": ClaimUnit.PERCENT.value,
        "direction": Direction.INCREASE.value,
        "quote": "We are guiding for an EBITDA margin of 19% to 20% for FY27.",
    }
    data.update(overrides)
    return DedupeKey(**data)


def test_dedupe_rules() -> None:
    base = key()
    # Same metric + period, overlapping range, different wording.
    assert is_duplicate(base, key(target_low=Decimal(20), target_high=Decimal(20), quote="x"))
    # Near-identical quote even with a different period resolution.
    assert is_duplicate(
        base,
        key(
            target_fiscal_year=None,
            quote="We are guiding for an EBITDA margin of 19% to 20% in FY27.",
        ),
    )
    # Templated sentences with different figures or periods stay separate.
    line_1 = key(
        metric_normalized="capacity",
        target_low=Decimal(40000),
        target_high=Decimal(40000),
        target_fiscal_quarter=1,
        quote="we expect the line 1 expansion to add roughly 40 thousand units of annual "
        "capacity once commissioning is complete in Q1 FY27.",
    )
    line_2 = key(
        metric_normalized="capacity",
        target_low=Decimal(45000),
        target_high=Decimal(45000),
        target_fiscal_quarter=2,
        quote="we expect the line 2 expansion to add roughly 45 thousand units of annual "
        "capacity once commissioning is complete in Q2 FY27.",
    )
    assert not is_duplicate(line_1, line_2)
    assert not is_duplicate(line_1, key(**{**line_1.__dict__, "target_fiscal_quarter": 2}))
    assert is_duplicate(
        line_1,
        key(**{**line_1.__dict__, "target_fiscal_quarter": None, "target_fiscal_year": None}),
    )
    # Different metric never merges.
    assert not is_duplicate(base, key(metric_normalized="gross_margin"))
    # Same period but disjoint targets.
    assert not is_duplicate(base, key(target_low=Decimal(22), target_high=Decimal(23), quote="y"))
    # Different period, different quote.
    assert not is_duplicate(base, key(target_fiscal_year=2028, quote="z"))
    # Both without targets: same direction merges, opposite does not.
    no_target = key(target_low=None, target_high=None, quote="margins will improve")
    assert is_duplicate(no_target, key(target_low=None, target_high=None, quote="other words"))
    assert not is_duplicate(
        no_target, key(target_low=None, target_high=None, direction="decrease", quote="w")
    )
