"""extract-v4 fixes: past results are not claims, revenue sub-lines are not total revenue,
scope (company / services / segment) keeps guidance apart, the judge sees the original
quote and key terms, and statements with no period stay out of the ledger and score."""

from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.models import ClaimType, Company, MetricNormalized, VerificationStatus
from app.schemas.chunk import SearchResult
from app.services.actuals_import import parse_actuals_csv
from app.services.claim_rules import is_past_result, revenue_line
from app.services.claim_schema import BatchClaim
from app.services.claim_validation import validate_claims
from app.services.deterministic_check import deterministic_check
from app.services.linking import thread_title
from app.services.scope import normalize_scope, scope_label
from app.services.verification import judge_prompt, key_terms
from tests.test_linking import groups_of
from tests.verification_factories import actuals, csv_rows, mention, ordinal, thread

# ------------------------------------------------------------ past results


@pytest.mark.parametrize(
    ("quote", "target", "call", "dropped"),
    [
        # Reported results on the call's own quarter or earlier: dropped.
        ("Our AI business grew 31% quarter on quarter.", (2027, 2), (2027, 2), True),
        ("Net new deals were 48% of the bookings this quarter.", (2027, 2), (2027, 2), True),
        ("The final dividend is up 12.4% over last year.", (2026, None), (2026, 4), True),
        ("Cloud revenue was 7.5% of total revenue in Q1.", (2027, 1), (2027, 2), True),
        # Forward-looking wording keeps a claim about the current period.
        ("We will close the year at 4.5% growth.", (2026, None), (2026, 4), False),
        ("We expect Q2 margins of 21%.", (2027, 2), (2027, 2), False),
        ("Our guidance for the quarter stays at 20-22%.", (2027, 2), (2027, 2), False),
        ("We are on track to hit 18% by FY26.", (2026, None), (2026, 4), False),
        # A future target period is never a past result.
        ("Revenue grew 5% and FY27 should be better.", (2027, None), (2026, 4), False),
        ("Margins were 19% for FY27 bookings.", (2027, None), (2026, 4), False),
    ],
)
def test_past_results_by_period(
    quote: str, target: tuple[int, int | None], call: tuple[int, int | None], dropped: bool
) -> None:
    reason = is_past_result(quote, ClaimType.NUMERIC, *target, *call)
    assert (reason is not None) is dropped


def test_no_period_past_tense_numbers_are_dropped_but_plans_are_not() -> None:
    assert is_past_result(
        "AI revenue was 8.2% of the total.", ClaimType.NUMERIC, None, None, 2027, 2
    )
    assert is_past_result("Net new grew to 55%.", ClaimType.DIRECTIONAL, None, None, 2027, 2)
    # Qualitative claims and present-tense commitments without a period stay.
    assert not is_past_result("We remain net cash.", ClaimType.QUALITATIVE, None, None, 2027, 2)
    assert not is_past_result("We will remain net cash.", ClaimType.NUMERIC, None, None, 2027, 2)


CHUNK = (
    "Our AI-led services grew 31% sequentially this quarter. For next year we expect revenue "
    "growth of 3% to 4% in constant currency."
)


def _batch(**overrides: Any) -> BatchClaim:
    data: dict[str, Any] = {
        "chunk_id": "C1",
        "quote": "Our AI-led services grew 31% sequentially this quarter.",
        "metric_raw": "AI-led services growth",
        "metric_normalized": "revenue_growth",
        "claim_type": "numeric",
        "target_low": 31,
        "target_high": 31,
        "unit": "percent",
        "direction": "increase",
        "timeframe_raw": "this quarter",
        "target_fiscal_year": 2027,
        "target_fiscal_quarter": 2,
        "confidence": 0.8,
        "scope": None,
    }
    data.update(overrides)
    return BatchClaim.model_validate(data)


def test_validation_drops_past_results_and_lists_them() -> None:
    guidance = _batch(
        quote="For next year we expect revenue growth of 3% to 4% in constant currency.",
        metric_raw="revenue growth",
        target_low=3,
        target_high=4,
        timeframe_raw="next year",
        target_fiscal_year=2028,
        target_fiscal_quarter=None,
    )
    outcome = validate_claims([_batch(), guidance], CHUNK, 2027, 2)
    assert [c.target_fiscal_year for c in outcome.accepted] == [2028]
    assert len(outcome.dropped_past) == 1
    dropped = outcome.dropped_past[0]
    assert dropped.period == "Q2FY27" and "31%" in dropped.quote
    assert outcome.rejected == 0  # a dropped past result is not a hallucinated quote


# ------------------------------------------------------------ scope / sub-lines


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "company"),
        ("Company", "company"),
        ("consolidated", "company"),
        ("services", "services"),
        ("segment: Services", "services"),
        ("segment:HCL Software", "segment:hcl-software"),
        ("Financial Services vertical", "segment:financial-services-vertical"),
    ],
)
def test_normalize_scope(raw: str | None, expected: str) -> None:
    assert normalize_scope(raw) == expected


def test_scope_labels() -> None:
    assert scope_label("company") is None
    assert scope_label("services") == "Services"
    assert scope_label("segment:hcl-software") == "Hcl software segment"


@pytest.mark.parametrize(
    ("metric_raw", "metric", "scope"),
    [
        ("third-party revenue", "other", "company"),
        ("Third party revenue growth", "other", "company"),
        ("segment revenue", "other", "company"),
        ("product revenue", "revenue", "segment:product"),
        ("software revenue growth", "revenue_growth", "segment:software"),
        ("services revenue growth", "revenue_growth", "services"),
        ("HCLTech services revenue growth", "revenue_growth", "services"),
        # Total revenue and unrelated wording stay company revenue.
        ("revenue growth", "revenue_growth", "company"),
        ("total revenue", "revenue", "company"),
        ("revenue from financial services clients", "revenue", "company"),
    ],
)
def test_revenue_sub_lines_are_not_total_revenue(metric_raw: str, metric: str, scope: str) -> None:
    got_metric, got_scope, _ = revenue_line(
        "revenue_growth" if "growth" in metric_raw.lower() else "revenue", metric_raw, "company"
    )
    assert (got_metric, got_scope) == (metric, scope)


def test_model_scope_is_kept_and_normalised() -> None:
    chunk = (
        "For FY26 we expect services revenue growth of 4.75% to 5.25% and company revenue "
        "growth of 4% to 4.5% in constant currency."
    )
    quote = chunk
    common: dict[str, Any] = {
        "quote": quote,
        "metric_normalized": "revenue_growth",
        "timeframe_raw": "FY26",
        "target_fiscal_year": 2026,
        "target_fiscal_quarter": None,
    }
    services = _batch(
        metric_raw="services revenue growth",
        target_low=4.75,
        target_high=5.25,
        scope="Services",
        **common,
    )
    company = _batch(metric_raw="revenue growth", target_low=4, target_high=4.5, **common)
    outcome = validate_claims([services, company], chunk, 2026, 3)
    assert [(c.scope, c.target_low) for c in outcome.accepted] == [
        ("services", Decimal("4.75")),
        ("company", Decimal("4")),
    ]


def test_services_and_company_guidance_never_share_a_thread() -> None:
    def growth(call: str, low: str, high: str, scope: str, quote: str) -> Any:
        return mention(
            call,
            metric="revenue_growth",
            low=low,
            high=high,
            unit="percent",
            target=(2026, None),
            quote=quote,
            scope=scope,
        )

    mentions = [
        growth("Q3FY26", "4.75", "5.25", "services", "services 4.75-5.25 (Q3)"),
        growth("Q3FY26", "4", "4.5", "company", "company 4-4.5 (Q3)"),
        growth("Q4FY26", "4.75", "5.25", "services", "services 4.75-5.25 (Q4)"),
        growth("Q4FY26", "4", "4.5", "company", "company 4-4.5 (Q4)"),
    ]
    assert groups_of(mentions) == [
        {"company 4-4.5 (Q3)", "company 4-4.5 (Q4)"},
        {"services 4.75-5.25 (Q3)", "services 4.75-5.25 (Q4)"},
    ]
    assert thread_title(mentions[0:1]) == "Services revenue growth 4.75–5.25% in FY26"
    assert thread_title(mentions[1:2]) == "Revenue growth 4–4.5% in FY26"


def test_services_claim_is_verified_only_against_services_actuals() -> None:
    claim = mention(
        "Q3FY26",
        metric="revenue_growth",
        low="4.75",
        high="5.25",
        unit="percent",
        target=(2026, None),
        scope="services",
    )
    company_only = actuals(("revenue_growth", 2026, None, "4.2", "percent"))
    outcome = deterministic_check(thread(claim), company_only, ordinal("Q4FY26"))
    assert outcome.status is VerificationStatus.UNVERIFIABLE
    assert "Services revenue growth" in outcome.reasoning

    both = actuals(
        ("revenue_growth", 2026, None, "4.2", "percent"),
        ("revenue_growth", 2026, None, "4.9", "percent", "services"),
    )
    outcome = deterministic_check(thread(claim), both, ordinal("Q4FY26"))
    assert outcome.status is VerificationStatus.DELIVERED


def test_actuals_csv_scope_column_is_optional() -> None:
    company = Company(name="Acme", nse_symbol="ACME")
    without = csv_rows("ACME", "revenue_growth,2026,,4.2,percent,release,3")
    rows, errors = parse_actuals_csv(without.encode(), company)
    assert errors == [] and rows[0].scope == "company"

    with_scope = (
        "nse_symbol,metric_normalized,fiscal_year,fiscal_quarter,value,unit,source_note,"
        "source_page,scope\n"
        "ACME,revenue_growth,2026,,4.2,percent,release,3,\n"
        "ACME,revenue_growth,2026,,4.9,percent,release,3,services\n"
        "ACME,revenue_growth,2026,,5.0,percent,release,3,Services\n"
    )
    rows, errors = parse_actuals_csv(with_scope.encode(), company)
    assert [r.scope for r in rows] == ["company", "services"]
    assert [(e.row, e.message) for e in errors] == [
        (4, "Duplicate of row 3 (same metric, scope and period).")
    ]


async def test_scoped_actuals_import_alongside_company_ones(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    from tests.factories import create_company

    company = await create_company(client, nse_symbol="SCOPED")
    content = (
        "nse_symbol,metric_normalized,fiscal_year,fiscal_quarter,value,unit,source_note,"
        "source_page,scope\n"
        "SCOPED,revenue_growth,2026,,4.2,percent,release,3,company\n"
        "SCOPED,revenue_growth,2026,,4.9,percent,release,3,services\n"
    )
    response = await client.post(
        f"/companies/{company['id']}/actuals/import",
        files={"file": ("a.csv", content.encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    listed = (await client.get(f"/companies/{company['id']}/actuals")).json()["actuals"]
    reported = sorted((a["scope"], a["value"]) for a in listed if not a["derived"])
    assert reported == [("company", "4.2"), ("services", "4.9")]


# ------------------------------------------------------------------ judge


def test_judge_prompt_names_the_original_quote_and_key_terms() -> None:
    first = mention(
        "Q3FY26",
        metric="other",
        claim_type=ClaimType.QUALITATIVE,
        target=(2027, None),
        quote="The $2.1 billion Verdant mega-deal will start ramping up in Q1.",
        metric_raw="Verdant mega-deal ramp-up",
    )
    later = mention(
        "Q4FY26",
        metric="other",
        claim_type=ClaimType.QUALITATIVE,
        target=(2027, None),
        quote="The Verdant deal remains on track.",
        metric_raw="Verdant mega-deal ramp-up",
    )
    view = thread(first, later)
    terms = key_terms(view)
    assert "verdant" in terms and "$2.1 billion" in terms
    evidence = [
        SearchResult(
            chunk_id=first.claim_id,
            document_id=first.document_id,
            period_label="Q1FY27",
            speaker="CEO",
            speaker_role="management",
            section="qna",
            page_start=4,
            page_end=4,
            text="We signed another large deal this quarter.",
            score=0.8,
        )
    ]
    user, _ = judge_prompt(view, evidence, "Q1FY27")
    assert 'ORIGINAL QUOTE (Q3FY26 call): "The $2.1 billion Verdant mega-deal' in user
    assert "KEY TERMS:" in user and "verdant" in user
    assert "different deal, project or product" in user


# --------------------------------------------------------- long-term threads


async def test_long_term_threads_leave_the_ledger_and_score(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    from sqlalchemy import update

    from app.models import ClaimThread
    from scripts.seed_demo import seed_demo

    company_id = await seed_demo(app.state.sessionmaker, app.state.settings)
    before = (await client.get(f"/companies/{company_id}/scorecard")).json()
    ledger = (await client.get(f"/companies/{company_id}/ledger")).json()
    title = ledger["rows"][0]["title"]
    async with app.state.sessionmaker() as session:
        await session.execute(
            update(ClaimThread)
            .where(ClaimThread.company_id == company_id, ClaimThread.title == title)
            .values(target_fiscal_year=None, target_fiscal_quarter=None)
        )
        await session.commit()

    card = (await client.get(f"/companies/{company_id}/scorecard")).json()
    assert [t["title"] for t in card["long_term"]] == [title]
    assert card["threads_total"] == before["threads_total"] - 1

    hidden = (await client.get(f"/companies/{company_id}/ledger")).json()
    assert title not in {r["title"] for r in hidden["rows"]}
    assert hidden["long_term_hidden"] == 1
    shown = (
        await client.get(f"/companies/{company_id}/ledger", params={"include_long_term": "true"})
    ).json()
    assert title in {r["title"] for r in shown["rows"]} and shown["long_term_hidden"] == 0


def test_metric_enum_unchanged() -> None:
    # Sub-lines reuse "other" or a scope; no new metric is needed.
    assert MetricNormalized("other") is MetricNormalized.OTHER


def test_negative_percent_put_down_to_a_cause_is_an_impact() -> None:
    from app.services.claim_rules import reclassify

    quote = "For FY'27, we see a half a percent reduction due to two clients."
    result = reclassify(
        "revenue_growth", "revenue growth", quote, "percent", ClaimType.NUMERIC, "company", -0.5
    )
    assert result.claim_type is ClaimType.IMPACT
    # A guided decline is still a target.
    guided = reclassify(
        "revenue_growth",
        "revenue growth",
        "We expect revenue to decline 2% next year.",
        "percent",
        ClaimType.NUMERIC,
        "company",
        -2,
    )
    assert guided.claim_type is ClaimType.NUMERIC


def test_acquired_revenue_is_not_total_revenue() -> None:
    metric, scope, note = revenue_line(
        "revenue", "Jasper acquisition revenue contribution", "company"
    )
    assert (metric, scope) == ("other", "company") and note is not None
