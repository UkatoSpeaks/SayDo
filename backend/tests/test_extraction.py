"""Claim extraction pipeline and the claims API (DB-backed, FakeLLMProvider)."""

import re
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.models import Chunk, Claim, ClaimType, Direction, ExtractionRun, ExtractionRunStatus
from app.services.extraction import extract_claims, has_forward_looking_signal
from app.services.extraction_prompt import FEW_SHOTS, PROMPT_VERSION, build_system_prompt
from app.services.llm import FakeLLMProvider, LLMQuotaExhausted, Message
from tests.concall_fixture import build_concall_pdf
from tests.factories import create_company, make_pdf, upload_pdf
from tests.test_processing import RESULTS_PAGES

MANAGEMENT = {"Rajesh Kumar", "Anita Desai", "Vikram Rao"}
ANALYST_NAMES = {"Priya Shah", "Nikhil Jain", "Sanjay Mehta"}

MARGIN_QUOTE = "We are guiding for an EBITDA margin of 19% to 20% for FY27."
NET_DEBT_QUOTE = "we target to become net debt free by the end of FY27."
REVENUE_PREPARED = "For FY27, we expect revenue growth of 15% to 18%, driven by new capacity"
REVENUE_LONG_ANSWER = (
    "So, to summarise, we remain confident of delivering revenue growth of 15% to 18% in FY27"
)


def claim_json(quote: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "quote": quote,
        "metric_raw": "metric",
        "metric_normalized": "other",
        "claim_type": "qualitative",
        "target_low": None,
        "target_high": None,
        "unit": None,
        "direction": "none",
        "timeframe_raw": None,
        "target_fiscal_year": None,
        "target_fiscal_quarter": None,
        "confidence": 0.8,
    }
    data.update(overrides)
    return data


def chunk_of(messages: list[Message]) -> str:
    """The CHUNK part of a user prompt (without the context block)."""
    user = messages[1]["content"]
    return user.split("\nCHUNK (", 1)[1] if "\nCHUNK (" in user else user.split("CHUNK (", 1)[1]


def default_responder(messages: list[Message]) -> dict[str, Any]:
    chunk = " ".join(chunk_of(messages).split())
    claims = []
    if MARGIN_QUOTE in chunk:
        claims.append(
            claim_json(
                MARGIN_QUOTE,
                metric_raw="EBITDA margin",
                metric_normalized="ebitda_margin",
                claim_type="numeric",
                target_low=19,
                target_high=20,
                unit="percent",
                direction="increase",
                timeframe_raw="for FY27",
                target_fiscal_year=2027,
                confidence=0.95,
            )
        )
    if NET_DEBT_QUOTE in chunk:
        claims.append(
            claim_json(
                NET_DEBT_QUOTE,
                metric_raw="net debt",
                metric_normalized="net_debt",
                claim_type="numeric",
                target_low=0,
                target_high=0,
                unit="inr_crore",
                direction="decrease",
                timeframe_raw="by the end of FY27",
                target_fiscal_year=2027,
                confidence=0.85,
            )
        )
    for quote, confidence in ((REVENUE_PREPARED, 0.8), (REVENUE_LONG_ANSWER, 0.9)):
        if quote in chunk:
            claims.append(
                claim_json(
                    quote,
                    metric_raw="revenue growth",
                    metric_normalized="revenue_growth",
                    claim_type="numeric",
                    target_low=15,
                    target_high=18,
                    unit="percent",
                    direction="increase",
                    timeframe_raw="FY27" if quote == REVENUE_PREPARED else "in FY27",
                    target_fiscal_year=2027,
                    confidence=confidence,
                )
            )
    return {"claims": claims}


@pytest.fixture
async def company(client: httpx.AsyncClient) -> dict[str, Any]:
    return await create_company(client)


@pytest.fixture(scope="module")
def concall_pdf() -> bytes:
    return build_concall_pdf()


@pytest.fixture
def llm(app: FastAPI) -> FakeLLMProvider:
    fake = FakeLLMProvider(responder=default_responder, default_model="openai/gpt-oss-120b")
    app.state.llm = fake
    return fake


@pytest.fixture
async def document(
    client: httpx.AsyncClient, company: dict[str, Any], concall_pdf: bytes
) -> dict[str, Any]:
    response = await upload_pdf(client, company["id"], concall_pdf)
    assert response.status_code == 201, response.text
    doc: dict[str, Any] = response.json()
    assert (await client.get(f"/documents/{doc['id']}")).json()["status"] == "embedded"
    return doc


async def _extract(client: httpx.AsyncClient, document_id: str, **params: Any) -> dict[str, Any]:
    response = await client.post(f"/documents/{document_id}/extract", params=params)
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def _runs(client: httpx.AsyncClient, document_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/documents/{document_id}/extraction-runs")
    assert response.status_code == 200, response.text
    body: list[dict[str, Any]] = response.json()
    return body


async def _claims(app: FastAPI, document_id: str) -> list[Claim]:
    async with app.state.sessionmaker() as session:
        rows = await session.scalars(
            select(Claim)
            .where(Claim.document_id == uuid.UUID(document_id))
            .order_by(Claim.page_number, Claim.quote)
        )
        return list(rows)


def _speaker(messages: list[Message]) -> str:
    match = re.match(r"([^,]+), management", chunk_of(messages))
    assert match is not None
    return match.group(1)


# ----------------------------------------------------------------- pipeline


async def test_extract_runs_in_background_and_stores_claims(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    started = await _extract(client, document["id"])
    assert started["status"] == "running"
    assert started["model"] == "openai/gpt-oss-120b"
    assert started["prompt_version"] == PROMPT_VERSION

    [run] = await _runs(client, document["id"])
    assert run["id"] == started["id"]
    assert run["status"] == "completed" and run["finished_at"] is not None
    # 8 management chunks; Vikram Rao's safe-harbour intro has no forward-looking keyword.
    assert run["chunks_total"] == 8
    assert run["chunks_skipped"] == 1
    assert run["chunks_processed"] == 7
    assert run["claims_created"] == 3  # margin, net debt, revenue (mentioned twice)
    assert run["claims_rejected"] == 0
    assert run["tokens_in"] > 0 and run["tokens_out"] > 0
    assert (await client.get(f"/documents/{document['id']}")).json()["status"] == "extracted"

    claims = {c.metric_normalized: c for c in await _claims(app, document["id"])}
    assert set(claims) == {"ebitda_margin", "net_debt", "revenue_growth"}
    margin = claims["ebitda_margin"]
    assert margin.quote == MARGIN_QUOTE and margin.speaker == "Anita Desai"
    assert (margin.target_low, margin.target_high) == (Decimal(19), Decimal(20))
    assert margin.claim_type is ClaimType.NUMERIC and margin.direction is Direction.INCREASE
    assert (margin.target_fiscal_year, margin.target_fiscal_quarter) == (2027, None)
    assert margin.model == "openai/gpt-oss-120b" and margin.prompt_version == PROMPT_VERSION
    assert margin.extraction_run_id == uuid.UUID(run["id"])
    assert margin.page_number == 3 and margin.mention_count == 1
    assert claims["net_debt"].page_number == 2

    async with app.state.sessionmaker() as session:
        pending = await session.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(
                Chunk.document_id == uuid.UUID(document["id"]),
                Chunk.speaker_role == "management",
                Chunk.claims_extracted_at.is_(None),
            )
        )
    assert pending == 0


async def test_only_management_chunks_are_sent_with_analyst_context(
    client: httpx.AsyncClient, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    await _extract(client, document["id"])
    assert len(llm.calls) == 7
    assert {_speaker(call.messages) for call in llm.calls} <= MANAGEMENT
    for call in llm.calls:
        chunk = chunk_of(call.messages)
        assert not any(name in chunk.split("\n", 1)[0] for name in ANALYST_NAMES)
        # Analyst questions never appear in the CHUNK block itself.
        assert "What is the EBITDA margin guidance" not in chunk

    margin_call = next(c for c in llm.calls if MARGIN_QUOTE in " ".join(c.user.split()))
    assert margin_call.user.startswith("CONTEXT (question from Priya Shah, analyst")
    assert "What is the EBITDA" in margin_call.user.split("CHUNK (")[0]

    # Both pieces of the long answer get Nikhil's question as context.
    long_answer = [c for c in llm.calls if "Nikhil" in c.user.split("CHUNK (")[0]]
    assert len(long_answer) == 2

    # Prepared remarks and the closing remark (after a moderator turn) have no context.
    no_context = [c for c in llm.calls if not c.user.startswith("CONTEXT")]
    assert {_speaker(c.messages) for c in no_context} == {"Rajesh Kumar", "Anita Desai"}
    assert len(no_context) == 3

    system = llm.calls[0].system
    assert "Acme Industries Ltd" in system and "Q2FY26" in system
    assert '"this year" / "current year" / "this fiscal" = FY26' in system
    assert '"next year" = FY27' in system and '"next quarter" = Q3FY26' in system


async def test_claims_quoting_the_analyst_are_rejected(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    def responder(messages: list[Message]) -> dict[str, Any]:
        if messages[1]["content"].startswith("CONTEXT (question from Priya Shah"):
            quote = "What is the EBITDA margin guidance for FY27"
            return {"claims": [claim_json(quote, metric_normalized="ebitda_margin")]}
        return {"claims": []}

    llm.responder = responder
    await _extract(client, document["id"])
    [run] = await _runs(client, document["id"])
    assert run["claims_rejected"] == 1 and run["claims_created"] == 0
    assert await _claims(app, document["id"]) == []


async def test_prefilter_can_be_disabled(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    app.state.settings = app.state.settings.model_copy(update={"extraction_prefilter": False})
    await _extract(client, document["id"])
    [run] = await _runs(client, document["id"])
    assert (run["chunks_skipped"], run["chunks_processed"]) == (0, 8)
    assert "Vikram Rao" in {_speaker(c.messages) for c in llm.calls}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We expect volumes to grow.", True),
        ("Our guidance remains unchanged.", True),
        ("We'll add two plants.", True),
        ("Capex for the year is Rs. 200 crore.", True),
        ("The line will be commissioned in H2.", True),
        ("Margins should sustain at these levels.", True),
        ("Thank you, and good evening everyone.", False),
        ("Revenue grew 12% this quarter to Rs. 1,250 crore.", False),
    ],
)
def test_forward_looking_signal(text: str, expected: bool) -> None:
    assert has_forward_looking_signal(text) is expected


async def test_numbers_not_in_source_are_downgraded_in_pipeline(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    def responder(messages: list[Message]) -> dict[str, Any]:
        if MARGIN_QUOTE in " ".join(chunk_of(messages).split()):
            return {
                "claims": [
                    claim_json(
                        MARGIN_QUOTE,
                        metric_normalized="ebitda_margin",
                        claim_type="numeric",
                        target_low=21,
                        target_high=21,
                        unit="percent",
                        confidence=0.9,
                    )
                ]
            }
        return {"claims": []}

    llm.responder = responder
    await _extract(client, document["id"])
    [claim] = await _claims(app, document["id"])
    assert claim.target_low is None and claim.claim_type is ClaimType.DIRECTIONAL
    assert claim.extraction_confidence == pytest.approx(0.54)
    assert claim.validation_notes is not None and "numbers not found" in claim.validation_notes


async def test_duplicates_are_merged_with_mention_count(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    await _extract(client, document["id"])
    revenue = [
        c for c in await _claims(app, document["id"]) if c.metric_normalized == "revenue_growth"
    ]
    assert len(revenue) == 1
    [claim] = revenue
    assert claim.mention_count == 2
    # The higher-confidence mention (0.9, in the Q&A answer) wins.
    assert claim.extraction_confidence == pytest.approx(0.9)
    assert claim.quote.startswith("So, to summarise")
    async with app.state.sessionmaker() as session:
        chunk = await session.get(Chunk, claim.chunk_id)
    assert chunk is not None and chunk.section.value == "qna"


async def test_chunk_overlap_is_not_a_second_mention(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    # The long answer is split into two chunks that overlap; this sentence is in both.
    quote = "On the machining side, we have committed capex of Rs. 120 crore"
    seen: list[str] = []

    def responder(messages: list[Message]) -> dict[str, Any]:
        if quote in " ".join(chunk_of(messages).split()):
            seen.append(messages[1]["content"])
            return {
                "claims": [
                    claim_json(
                        quote,
                        metric_normalized="capex",
                        claim_type="numeric",
                        target_low=120,
                        target_high=120,
                        unit="inr_crore",
                    )
                ]
            }
        return {"claims": []}

    llm.responder = responder
    await _extract(client, document["id"])
    assert len(seen) == 2  # the overlap really exists
    [claim] = await _claims(app, document["id"])
    assert claim.mention_count == 1


async def test_daily_quota_pauses_and_resume_continues(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    calls_before_quota = 2
    state = {"calls": 0}

    def responder(messages: list[Message]) -> Any:
        state["calls"] += 1
        if state["calls"] == calls_before_quota + 1:
            return LLMQuotaExhausted("Groq daily quota exhausted: tokens per day (TPD)")
        return default_responder(messages)

    llm.responder = responder
    paused = await _extract(client, document["id"])
    [run] = await _runs(client, document["id"])
    assert run["status"] == "paused_quota"
    assert "daily quota" in run["error_message"]
    assert run["chunks_processed"] == calls_before_quota
    assert (await client.get(f"/documents/{document['id']}")).json()["status"] == "embedded"
    first_pass = [chunk_of(c.messages) for c in llm.calls]
    claims_after_pause = len(await _claims(app, document["id"]))

    llm.responder = default_responder
    llm.calls.clear()
    resumed = await _extract(client, document["id"])
    runs = await _runs(client, document["id"])
    assert [r["id"] for r in runs] == [resumed["id"], paused["id"]]
    assert runs[0]["status"] == "completed"
    # Only the chunks not yet handled: the one that hit the quota and the rest.
    assert runs[0]["chunks_total"] == 8 - calls_before_quota - 1  # minus the skipped one
    resumed_chunks = [chunk_of(c.messages) for c in llm.calls]
    assert resumed_chunks[0] == first_pass[-1]
    assert not set(resumed_chunks) & set(first_pass[:-1])
    assert len(await _claims(app, document["id"])) == 3 >= claims_after_pause
    assert (await client.get(f"/documents/{document['id']}")).json()["status"] == "extracted"


async def test_invalid_output_fails_only_that_chunk(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    def responder(messages: list[Message]) -> Any:
        if MARGIN_QUOTE in " ".join(chunk_of(messages).split()):
            return '{"claims": "broken"}'
        return default_responder(messages)

    llm.responder = responder
    await _extract(client, document["id"])
    [run] = await _runs(client, document["id"])
    assert run["status"] == "failed"
    assert "1 chunk(s) returned invalid output" in run["error_message"]
    assert run["chunks_processed"] == 6
    assert (await client.get(f"/documents/{document['id']}")).json()["status"] == "embedded"

    llm.responder = default_responder
    llm.calls.clear()
    await _extract(client, document["id"])
    assert len(llm.calls) == 1  # only the failed chunk is retried
    assert (await _runs(client, document["id"]))[0]["status"] == "completed"
    assert "ebitda_margin" in {c.metric_normalized for c in await _claims(app, document["id"])}


async def test_reextract_deletes_and_rebuilds(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    await _extract(client, document["id"])
    before = await _claims(app, document["id"])

    again = await client.post(f"/documents/{document['id']}/extract")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_extracted"

    llm.calls.clear()
    await _extract(client, document["id"], reextract="true")
    after = await _claims(app, document["id"])
    assert len(llm.calls) == 7
    assert len(after) == len(before)
    assert not {c.id for c in before} & {c.id for c in after}
    assert [c.mention_count for c in after] == [c.mention_count for c in before]
    runs = await _runs(client, document["id"])
    assert [r["status"] for r in runs] == ["completed", "completed"]
    assert {c.extraction_run_id for c in after} == {uuid.UUID(runs[0]["id"])}


async def test_rechunk_drops_claims(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    await _extract(client, document["id"])
    assert await _claims(app, document["id"])
    response = await client.post(f"/documents/{document['id']}/process", params={"rechunk": "true"})
    assert response.status_code == 202
    assert await _claims(app, document["id"]) == []
    assert (await client.get(f"/documents/{document['id']}")).json()["status"] == "embedded"


async def test_extract_conflicts_and_not_found(
    client: httpx.AsyncClient,
    app: FastAPI,
    llm: FakeLLMProvider,
    document: dict[str, Any],
    company: dict[str, Any],
) -> None:
    app.state.processing.try_start(uuid.UUID(document["id"]))
    busy = await client.post(f"/documents/{document['id']}/extract")
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "extraction_in_progress"
    assert await _runs(client, document["id"]) == []
    app.state.processing.finish(uuid.UUID(document["id"]))

    results = await upload_pdf(
        client, company["id"], make_pdf(RESULTS_PAGES), doc_type="results", period_label="Q1FY26"
    )
    not_ready = await client.post(f"/documents/{results.json()['id']}/extract")
    assert not_ready.status_code == 409
    assert not_ready.json()["error"]["code"] == "document_not_ready"

    assert (await client.post(f"/documents/{uuid.uuid4()}/extract")).status_code == 404
    assert (await client.get(f"/documents/{uuid.uuid4()}/extraction-runs")).status_code == 404


async def test_stale_running_row_does_not_block(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    async with app.state.sessionmaker() as session:
        session.add(
            ExtractionRun(
                document_id=uuid.UUID(document["id"]), model="m", prompt_version=PROMPT_VERSION
            )
        )
        await session.commit()
    await _extract(client, document["id"])
    runs = await _runs(client, document["id"])
    assert sorted(r["status"] for r in runs) == ["completed", "failed"]
    stale = next(r for r in runs if r["status"] == "failed")
    assert "Interrupted" in stale["error_message"]


async def test_auto_extract_after_processing(
    client: httpx.AsyncClient,
    app: FastAPI,
    llm: FakeLLMProvider,
    company: dict[str, Any],
    concall_pdf: bytes,
) -> None:
    app.state.settings = app.state.settings.model_copy(update={"auto_extract": True})
    response = await upload_pdf(client, company["id"], concall_pdf)
    doc = response.json()
    assert (await client.get(f"/documents/{doc['id']}")).json()["status"] == "extracted"
    [run] = await _runs(client, doc["id"])
    assert run["status"] == "completed" and run["claims_created"] == 3


async def test_auto_extract_is_off_by_default(
    client: httpx.AsyncClient, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    assert llm.calls == []
    assert await _runs(client, document["id"]) == []


async def test_extract_claims_function(
    app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    run = await extract_claims(
        app.state.sessionmaker, llm, uuid.UUID(document["id"]), settings=app.state.settings
    )
    assert run.status is ExtractionRunStatus.COMPLETED and run.claims_created == 3


# ---------------------------------------------------------------- claims API


async def _seed_claims(
    app: FastAPI, client: httpx.AsyncClient, company: dict[str, Any]
) -> dict[str, Any]:
    """Two calls (Q4FY25 and Q2FY26) with hand-made claims."""
    q2 = await upload_pdf(client, company["id"], make_pdf(["Q2 call text"]), period_label="Q2FY26")
    q4 = await upload_pdf(
        client, company["id"], make_pdf(["Q4 call text", "page two"]), period_label="Q4FY25"
    )
    docs = {"Q2FY26": q2.json(), "Q4FY25": q4.json()}
    rows = [
        # (period, page, metric, type, target_fy, confidence)
        ("Q2FY26", 3, "ebitda_margin", ClaimType.NUMERIC, 2027, 0.95),
        ("Q2FY26", 1, "revenue_growth", ClaimType.NUMERIC, 2027, 0.8),
        ("Q2FY26", 2, "capex", ClaimType.QUALITATIVE, None, 0.5),
        ("Q4FY25", 2, "ebitda_margin", ClaimType.DIRECTIONAL, 2026, 0.7),
        ("Q4FY25", 1, "volume_growth", ClaimType.DIRECTIONAL, 2026, 0.9),
    ]
    async with app.state.sessionmaker() as session:
        for period, page, metric, claim_type, target_fy, confidence in rows:
            session.add(
                Claim(
                    company_id=uuid.UUID(company["id"]),
                    document_id=uuid.UUID(docs[period]["id"]),
                    metric_raw=metric,
                    metric_normalized=metric,
                    claim_type=claim_type,
                    quote=f"{period} {metric} quote",
                    page_number=page,
                    target_fiscal_year=target_fy,
                    extraction_confidence=confidence,
                    target_low=Decimal(40000) if metric == "volume_growth" else None,
                    target_high=Decimal(40000) if metric == "volume_growth" else None,
                )
            )
        await session.commit()
    return docs


async def test_company_claims_sorted_and_filtered(
    client: httpx.AsyncClient, app: FastAPI, company: dict[str, Any]
) -> None:
    await _seed_claims(app, client, company)
    url = f"/companies/{company['id']}/claims"

    async def get(**params: Any) -> dict[str, Any]:
        response = await client.get(url, params=params)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    everything = await get()
    assert everything["total"] == 5
    assert [(c["period_label"], c["page_number"]) for c in everything["claims"]] == [
        ("Q4FY25", 1),
        ("Q4FY25", 2),
        ("Q2FY26", 1),
        ("Q2FY26", 2),
        ("Q2FY26", 3),
    ]
    # asyncpg returns numeric 40000 as Decimal("4E+4"); the API must not leak that.
    assert everything["claims"][0]["target_low"] == "40000"
    assert {c["metric_normalized"] for c in (await get(metric="ebitda_margin"))["claims"]} == {
        "ebitda_margin"
    }
    assert (await get(metric="ebitda_margin"))["total"] == 2
    assert (await get(claim_type="directional"))["total"] == 2
    assert (await get(period_from="Q1FY26"))["total"] == 3
    assert (await get(period_to="FY25"))["total"] == 2
    assert (await get(period_from="Q4FY25", period_to="Q4FY25"))["total"] == 2
    assert (await get(target_fy="FY27"))["total"] == 2
    assert (await get(target_fy="2026"))["total"] == 2
    assert (await get(target_fy="26"))["total"] == 2
    assert (await get(min_confidence=0.85))["total"] == 2
    assert (await get(metric="ebitda_margin", period_from="Q2FY26"))["total"] == 1

    page = await get(limit=2, offset=1)
    assert page["total"] == 5 and page["limit"] == 2 and page["offset"] == 1
    assert [c["id"] for c in page["claims"]] == [c["id"] for c in everything["claims"][1:3]]

    for params in (
        {"metric": "headcount"},
        {"claim_type": "promise"},
        {"period_from": "Q5FY26"},
        {"period_from": "Q2FY26", "period_to": "Q1FY26"},
        {"target_fy": "Q2FY27"},
        {"min_confidence": 2},
        {"limit": 0},
    ):
        assert (await client.get(url, params=params)).status_code == 422, params
    assert (await client.get(f"/companies/{uuid.uuid4()}/claims")).status_code == 404


async def test_claim_detail_includes_source(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    await _extract(client, document["id"])
    margin = next(
        c for c in await _claims(app, document["id"]) if c.metric_normalized == "ebitda_margin"
    )
    response = await client.get(f"/claims/{margin.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quote"] == MARGIN_QUOTE
    assert body["period_label"] == "Q2FY26" and body["page_number"] == 3
    assert body["document"]["period_label"] == "Q2FY26"
    assert body["document"]["fiscal_year"] == 2026 and body["document"]["fiscal_quarter"] == 2
    assert body["chunk"]["speaker"] == "Anita Desai"
    assert body["chunk"]["speaker_role"] == "management"
    assert MARGIN_QUOTE in " ".join(body["chunk"]["text"].split())
    assert body["target_low"] == "19" and body["mention_count"] == 1
    assert (await client.get(f"/claims/{uuid.uuid4()}")).status_code == 404


# ------------------------------------------------------------------ prompt


def test_system_prompt_examples_are_self_consistent() -> None:
    prompt = build_system_prompt("Acme", 2026, 2)
    assert prompt.count("### Example") == len(FEW_SHOTS) == 5
    assert any(shot.claims == [] for shot in FEW_SHOTS)
    for shot in FEW_SHOTS:
        for claim in shot.claims:
            assert str(claim["quote"]) in shot.text  # few-shot quotes are verbatim too
    q4 = build_system_prompt("Acme", 2026, 4)
    assert '"this fiscal" = FY27' in q4 and '"next quarter" = Q1FY27' in q4
