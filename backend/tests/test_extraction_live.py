"""Calls the real Groq API. Run with: uv run pytest -m live -s tests/test_extraction_live.py"""

import time

import pytest

from app.core.config import Settings
from app.models import ChunkSection, ClaimType, MetricNormalized
from app.services.claim_schema import BatchExtractionResult
from app.services.claim_validation import validate_claims
from app.services.extraction_prompt import (
    SYSTEM_PROMPT,
    PromptChunk,
    PromptQuestion,
    build_user_prompt,
)
from app.services.llm import GroqProvider, create_llm_provider

pytestmark = pytest.mark.live

GUIDANCE_CHUNK = (
    "Thanks, Priya. For FY27 we are guiding for revenue growth of 15% to 18%, and we expect "
    "EBITDA margin to improve to around 20% by the fourth quarter of next year as the new "
    "Sanand line ramps up. Revenue this quarter grew 11%, which was a bit below our own "
    "expectations because of the extended monsoon."
)
GUIDANCE_CONTEXT = "Could you give us your growth and margin outlook for next year?"
NO_CLAIM_CHUNK = (
    "Thank you, Rahul. Revenue for the quarter was Rs. 1,250 crore, up 18% year-on-year, and "
    "EBITDA margin was 19.4%. Profit after tax grew 22% to Rs. 141 crore. I would like to "
    "thank our employees and partners for their hard work during the quarter."
)


async def test_groq_extracts_a_batch_of_two_handcrafted_chunks() -> None:
    settings = Settings()
    if settings.groq_api_key is None:
        pytest.skip("GROQ_API_KEY is not set")
    provider = create_llm_provider(settings.model_copy(update={"llm_provider": "groq"}))
    assert isinstance(provider, GroqProvider)
    models = await provider.list_models()
    assert settings.extraction_model in models, models

    user = build_user_prompt(
        company="Acme Industries Ltd",
        call_fiscal_year=2026,
        call_fiscal_quarter=2,
        chunks=[
            PromptChunk("C1", GUIDANCE_CHUNK, "Anita Desai", ChunkSection.QNA, question=0),
            PromptChunk("C2", NO_CLAIM_CHUNK, "Anita Desai", ChunkSection.PREPARED_REMARKS),
        ],
        questions=[PromptQuestion(GUIDANCE_CONTEXT, "Priya Shah")],
    )
    started = time.perf_counter()
    result, usage = await provider.complete_json(SYSTEM_PROMPT, user, BatchExtractionResult)
    latency = time.perf_counter() - started
    guidance = [c for c in result.claims if c.chunk_id == "C1"]
    none = [c for c in result.claims if c.chunk_id != "C1"]

    outcome = validate_claims(list(guidance), GUIDANCE_CHUNK, 2026, 2)
    print(f"\nmodel={settings.extraction_model} latency={latency:.1f}s")
    print(f"usage: {usage}")
    for claim in outcome.accepted:
        print(
            f"- {claim.metric_normalized.value} {claim.claim_type.value} "
            f"{claim.target_low}-{claim.target_high} {claim.unit} "
            f"{claim.timeframe_raw!r} -> "
            f"FY{claim.target_fiscal_year} Q{claim.target_fiscal_quarter} "
            f"conf={claim.confidence} notes={claim.notes}\n  {claim.quote!r}"
        )
    print(f"rejected={outcome.rejected} no-claim chunk returned {len(none)} claim(s)")

    assert outcome.rejected == 0
    by_metric = {c.metric_normalized: c for c in outcome.accepted}
    growth = by_metric[MetricNormalized.REVENUE_GROWTH]
    assert growth.claim_type is ClaimType.NUMERIC
    assert (float(growth.target_low or 0), float(growth.target_high or 0)) == (15, 18)
    assert growth.target_fiscal_year == 2027
    margin = by_metric[MetricNormalized.EBITDA_MARGIN]
    assert float(margin.target_low or 0) == 20
    assert (margin.target_fiscal_year, margin.target_fiscal_quarter) == (2027, 4)
    # The 11% is a reported result, not a claim.
    assert all("11%" not in c.quote for c in outcome.accepted if c.claim_type is ClaimType.NUMERIC)
    assert none == []
