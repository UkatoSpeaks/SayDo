"""Claim extraction pipeline and the claims API (DB-backed, FakeLLMProvider)."""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.models import Chunk, Claim, ClaimType, Direction, ExtractionRun, ExtractionRunStatus
from app.services import extraction_prompt_v1
from app.services.extraction import (
    extract_claims,
    has_forward_looking_signal,
    plan_batches,
    skip_reason,
)
from app.services.extraction_prompt import (
    EXAMPLES,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    call_line,
)
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


@dataclass
class SentChunk:
    """One management chunk as it appears in a request."""

    chunk_id: str | None  # None for extract-v1 (one chunk per request)
    speaker: str
    text: str  # whitespace collapsed
    question: str | None  # the analyst context it was sent with
    question_speaker: str | None


_V1_CHUNK_RE = re.compile(r"CHUNK \(([^,]+), management, [^)]+\):\n")
_V2_CHUNK_RE = re.compile(
    r"^\[(C\d+)\] ([^,\n]+), (?:Q&A|Prepared remarks)(?:, answers (Q\d+))?:\n", re.M
)
_V2_QUESTION_RE = re.compile(r"^(Q\d+) \(([^,]+), analyst; context only\):\n", re.M)


def _blocks(user: str, regex: re.Pattern[str]) -> list[tuple[re.Match[str], str]]:
    matches = list(_V2_CHUNK_RE.finditer(user)) + list(_V2_QUESTION_RE.finditer(user))
    matches.sort(key=lambda m: m.start())
    out = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(user)
        if match.re is regex:
            out.append((match, " ".join(user[match.end() : end].split())))
    return out


def sent_chunks(messages: list[Message]) -> list[SentChunk]:
    user = messages[1]["content"]
    v1 = _V1_CHUNK_RE.search(user)
    if v1 is not None:
        context = (
            user.split("\n", 1)[1].split("\n\nCHUNK (")[0] if user.startswith("CONTEXT") else None
        )
        who = re.match(r"CONTEXT \(question from ([^,]+),", user)
        return [
            SentChunk(
                None,
                v1.group(1),
                " ".join(user[v1.end() :].split()),
                " ".join(context.split()) if context else None,
                who.group(1) if who else None,
            )
        ]
    questions = {m.group(1): (text, m.group(2)) for m, text in _blocks(user, _V2_QUESTION_RE)}
    return [
        SentChunk(
            m.group(1),
            m.group(2),
            text,
            questions[m.group(3)][0] if m.group(3) else None,
            questions[m.group(3)][1] if m.group(3) else None,
        )
        for m, text in _blocks(user, _V2_CHUNK_RE)
    ]


def all_sent(llm: FakeLLMProvider) -> list[SentChunk]:
    return [chunk for call in llm.calls for chunk in sent_chunks(call.messages)]


ChunkResponder = Callable[[SentChunk], list[dict[str, Any]]]


def per_chunk(fn: ChunkResponder) -> Callable[[list[Message]], dict[str, Any]]:
    """A FakeLLMProvider responder that answers each chunk of a request with ``fn``."""

    def responder(messages: list[Message]) -> dict[str, Any]:
        claims = []
        for chunk in sent_chunks(messages):
            for claim in fn(chunk):
                claims.append({"chunk_id": chunk.chunk_id, **claim} if chunk.chunk_id else claim)
        return {"claims": claims}

    return responder


def default_claims(sent: SentChunk) -> list[dict[str, Any]]:
    chunk = sent.text
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
    return claims


default_responder = per_chunk(default_claims)


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


def _speakers(llm: FakeLLMProvider) -> set[str]:
    return {chunk.speaker for chunk in all_sent(llm)}


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


async def test_batches_are_sized_by_expected_output(
    client: httpx.AsyncClient, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    await _extract(client, document["id"])
    # Each piece of the long capacity answer holds more forward-looking sentences than
    # EXTRACTION_BATCH_MAX_OUTPUT_TOKENS allows in one request, so each goes alone.
    batches = [[c.question_speaker for c in sent_chunks(call.messages)] for call in llm.calls]
    assert batches == [
        [None, None, "Priya Shah"],
        ["Nikhil Jain"],
        ["Nikhil Jain"],
        ["Sanjay Mehta", None],
    ]


async def test_only_management_chunks_are_sent_in_batches_with_analyst_context(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"extraction_batch_max_output_tokens": 100_000}
    )
    await _extract(client, document["id"])
    # 7 chunks to send; at most 6 per request (EXTRACTION_BATCH_MAX_CHUNKS).
    assert [len(sent_chunks(c.messages)) for c in llm.calls] == [6, 1]
    sent = all_sent(llm)
    assert len(sent) == 7
    assert {c.chunk_id for c in sent_chunks(llm.calls[0].messages)} == {
        f"C{i}" for i in range(1, 7)
    }
    assert _speakers(llm) <= MANAGEMENT
    for chunk in sent:
        # Analyst questions never appear in a chunk block itself.
        assert "What is the EBITDA margin guidance" not in chunk.text

    margin = next(c for c in sent if MARGIN_QUOTE in c.text)
    assert margin.question_speaker == "Priya Shah"
    assert margin.question is not None and "What is the EBITDA" in margin.question

    # Both pieces of the long answer answer Nikhil's question, which is sent once.
    long_answer = [c for c in sent if c.question_speaker == "Nikhil Jain"]
    assert len(long_answer) == 2
    assert llm.calls[0].user.count("(Nikhil Jain, analyst; context only)") == 1

    # Prepared remarks and the closing remark (after a moderator turn) have no context.
    no_context = [c for c in sent if c.question is None]
    assert {c.speaker for c in no_context} == {"Rajesh Kumar", "Anita Desai"}
    assert len(no_context) == 3

    # The system prompt is identical on every call (a cacheable prefix); the call
    # details are in the user message.
    assert {c.system for c in llm.calls} == {SYSTEM_PROMPT}
    assert "Acme" not in SYSTEM_PROMPT
    for call in llm.calls:
        assert call.user.startswith(call_line("Acme Industries Ltd", 2026, 2))
    line = call_line("Acme Industries Ltd", 2026, 2)
    assert "Q2FY26" in line and '"this fiscal" = FY26 (2026)' in line
    assert '"next year" = FY27 (2027)' in line and '"next quarter" = Q3FY26' in line


async def test_claims_quoting_the_analyst_are_rejected(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    def claims(chunk: SentChunk) -> list[dict[str, Any]]:
        if chunk.question_speaker == "Priya Shah":
            quote = "What is the EBITDA margin guidance for FY27"
            return [claim_json(quote, metric_normalized="ebitda_margin")]
        return []

    llm.responder = per_chunk(claims)
    await _extract(client, document["id"])
    [run] = await _runs(client, document["id"])
    assert run["claims_rejected"] == 1 and run["claims_created"] == 0
    assert await _claims(app, document["id"]) == []


async def test_claim_attributed_to_the_wrong_chunk_is_rejected(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    """Quotes are checked against the chunk a claim names, not the whole batch."""

    def responder(messages: list[Message]) -> dict[str, Any]:
        chunks = sent_chunks(messages)
        margin = next((c for c in chunks if MARGIN_QUOTE in c.text), None)
        if margin is None:
            return {"claims": []}
        other = next(c for c in chunks if c.chunk_id != margin.chunk_id)
        margin_claim = claim_json(
            MARGIN_QUOTE,
            metric_normalized="ebitda_margin",
            claim_type="numeric",
            target_low=19,
            target_high=20,
            unit="percent",
        )
        return {
            "claims": [
                # The margin quote is in the batch, but not in the chunk it names.
                {"chunk_id": other.chunk_id, **margin_claim},
                # A chunk id that is not in the request at all.
                {"chunk_id": "C99", **margin_claim},
                # Correctly attributed.
                {"chunk_id": margin.chunk_id, **claim_json(NET_DEBT_QUOTE)}
                if NET_DEBT_QUOTE in margin.text
                else {"chunk_id": margin.chunk_id, **margin_claim},
            ]
        }

    llm.responder = responder
    await _extract(client, document["id"])
    [run] = await _runs(client, document["id"])
    assert run["status"] == "completed"
    assert run["claims_rejected"] == 2 and run["claims_created"] == 1
    [claim] = await _claims(app, document["id"])
    assert claim.quote == MARGIN_QUOTE and claim.speaker == "Anita Desai"
    async with app.state.sessionmaker() as session:
        chunk = await session.get(Chunk, claim.chunk_id)
    assert chunk is not None and MARGIN_QUOTE in " ".join(chunk.text.split())


async def test_v1_prompt_sends_one_chunk_per_request(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"extraction_prompt_version": "extract-v1", "extraction_strict_prefilter": False}
    )
    started = await _extract(client, document["id"])
    assert started["prompt_version"] == "extract-v1"
    [run] = await _runs(client, document["id"])
    assert run["status"] == "completed"
    assert (run["chunks_processed"], run["claims_created"]) == (7, 3)
    assert len(llm.calls) == 7
    assert all(len(sent_chunks(c.messages)) == 1 for c in llm.calls)
    assert "Acme Industries Ltd" in llm.calls[0].system
    assert {c.prompt_version for c in await _claims(app, document["id"])} == {"extract-v1"}


async def test_prefilter_can_be_disabled(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    app.state.settings = app.state.settings.model_copy(update={"extraction_prefilter": False})
    await _extract(client, document["id"])
    [run] = await _runs(client, document["id"])
    assert (run["chunks_skipped"], run["chunks_processed"]) == (0, 8)
    assert "Vikram Rao" in _speakers(llm)


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
    def claims(chunk: SentChunk) -> list[dict[str, Any]]:
        if MARGIN_QUOTE in chunk.text:
            return [
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
        return []

    llm.responder = per_chunk(claims)
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

    def claims(chunk: SentChunk) -> list[dict[str, Any]]:
        if quote in chunk.text:
            seen.append(chunk.text)
            return [
                claim_json(
                    quote,
                    metric_normalized="capex",
                    claim_type="numeric",
                    target_low=120,
                    target_high=120,
                    unit="inr_crore",
                )
            ]
        return []

    llm.responder = per_chunk(claims)
    await _extract(client, document["id"])
    assert len(seen) == 2  # the overlap really exists
    [claim] = await _claims(app, document["id"])
    assert claim.mention_count == 1


async def test_daily_quota_pauses_and_resume_continues(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    # Batches of 2 chunks: 7 chunks -> 4 requests.
    app.state.settings = app.state.settings.model_copy(
        update={"extraction_batch_max_chunks": 2, "extraction_batch_max_output_tokens": 100_000}
    )
    state = {"calls": 0}

    def responder(messages: list[Message]) -> Any:
        state["calls"] += 1
        if state["calls"] == 2:
            return LLMQuotaExhausted("Groq daily quota exhausted: tokens per day (TPD)")
        return default_responder(messages)

    llm.responder = responder
    paused = await _extract(client, document["id"])
    [run] = await _runs(client, document["id"])
    assert run["status"] == "paused_quota"
    assert "daily quota" in run["error_message"]
    # Only the first request's chunks are marked done; the failed batch is not.
    assert run["chunks_processed"] == 2
    assert (await client.get(f"/documents/{document['id']}")).json()["status"] == "embedded"
    done = [c.text for c in sent_chunks(llm.calls[0].messages)]
    hit_quota = [c.text for c in sent_chunks(llm.calls[1].messages)]
    claims_after_pause = len(await _claims(app, document["id"]))

    llm.responder = default_responder
    llm.calls.clear()
    resumed = await _extract(client, document["id"])
    runs = await _runs(client, document["id"])
    assert [r["id"] for r in runs] == [resumed["id"], paused["id"]]
    assert runs[0]["status"] == "completed"
    # The skipped chunk and the first batch are not pending any more.
    assert runs[0]["chunks_total"] == 8 - 1 - 2
    resumed_chunks = [c.text for c in all_sent(llm)]
    assert resumed_chunks[:2] == hit_quota
    assert not set(resumed_chunks) & set(done)
    assert len(await _claims(app, document["id"])) == 3 >= claims_after_pause
    assert (await client.get(f"/documents/{document['id']}")).json()["status"] == "extracted"


async def test_invalid_batch_is_retried_chunk_by_chunk(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    def responder(messages: list[Message]) -> Any:
        if any(MARGIN_QUOTE in c.text for c in sent_chunks(messages)):
            return '{"claims": "broken"}'
        return default_responder(messages)

    llm.responder = responder
    await _extract(client, document["id"])
    [run] = await _runs(client, document["id"])
    # Batch of 3: invalid twice (reply + repair); then 3 single-chunk requests, of which
    # the margin chunk fails twice again; then the remaining requests (1, 1, 2).
    assert [len(sent_chunks(c.messages)) for c in llm.calls] == [3, 3, 1, 1, 1, 1, 1, 1, 2]
    assert run["status"] == "failed"
    assert "1 chunk(s) returned invalid output" in run["error_message"]
    assert run["chunks_processed"] == 6
    assert (await client.get(f"/documents/{document['id']}")).json()["status"] == "embedded"
    # The other chunks of the failed batch were still extracted.
    assert {c.metric_normalized for c in await _claims(app, document["id"])} == {
        "net_debt",
        "revenue_growth",
    }

    llm.responder = default_responder
    llm.calls.clear()
    await _extract(client, document["id"])
    assert len(llm.calls) == 1  # only the failed chunk is retried
    assert (await _runs(client, document["id"]))[0]["status"] == "completed"
    assert "ebitda_margin" in {c.metric_normalized for c in await _claims(app, document["id"])}


async def test_truncated_batch_is_split_without_a_repair_retry(
    client: httpx.AsyncClient, app: FastAPI, llm: FakeLLMProvider, document: dict[str, Any]
) -> None:
    from app.services.llm import LLMTruncatedError

    def responder(messages: list[Message]) -> Any:
        if len(sent_chunks(messages)) > 1:
            return LLMTruncatedError("Groq reply hit max_completion_tokens=4096")
        return default_responder(messages)

    llm.responder = responder
    await _extract(client, document["id"])
    [run] = await _runs(client, document["id"])
    # Halved until single chunks get through; no repair request in between.
    assert [len(sent_chunks(c.messages)) for c in llm.calls] == [
        3, 1, 2, 1, 1,  # first batch: 3 -> 1 + 2 -> 1 + 1
        1, 1,  # the two long-answer pieces, alone already
        2, 1, 1,
    ]  # fmt: skip
    assert run["status"] == "completed" and run["claims_created"] == 3


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
    assert len(all_sent(llm)) == 7
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


def test_v1_prompt_is_unchanged_and_self_consistent() -> None:
    prompt = extraction_prompt_v1.build_system_prompt("Acme", 2026, 2)
    assert extraction_prompt_v1.PROMPT_VERSION == "extract-v1"
    assert prompt.count("### Example") == len(extraction_prompt_v1.FEW_SHOTS) == 5
    for shot in extraction_prompt_v1.FEW_SHOTS:
        for claim in shot.claims:
            assert str(claim["quote"]) in shot.text  # few-shot quotes are verbatim too
    q4 = extraction_prompt_v1.build_system_prompt("Acme", 2026, 4)
    assert '"this fiscal" = FY27' in q4 and '"next quarter" = Q1FY27' in q4


def test_v2_prompt_examples_are_self_consistent() -> None:
    assert PROMPT_VERSION == "extract-v2"
    assert SYSTEM_PROMPT.count("Example ") == len(EXAMPLES) == 3
    assert any(
        not [c for c in example.claims if c["chunk_id"] == f"C{i}"]
        for example in EXAMPLES
        for i in range(1, len(example.chunks) + 1)
    ), "one example chunk should have no claims"
    for example in EXAMPLES:
        for claim in example.claims:
            index = int(str(claim["chunk_id"])[1:]) - 1
            assert str(claim["quote"]) in example.chunks[index].text
    q4 = call_line("Acme", 2026, 4)
    assert '"this fiscal" = FY27 (2027)' in q4 and '"next quarter" = Q1FY27' in q4


# --------------------------------------------------------------- pre-filter


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        # Hand-overs and sign-offs: "will" / "next quarter" are not outlook.
        (
            "Thank you, Vikram, and good evening to all of you. I will now request our CFO, "
            "Ms. Anita Desai, to take you through the financials for the quarter and the half.",
            "pleasantries only",
        ),
        (
            "Thank you all for joining the call today and for your continued interest in the "
            "company. We look forward to speaking with you again next quarter. Have a good "
            "evening and stay safe.",
            "pleasantries only",
        ),
        (
            "Sure, Sanjay, that is a fair point and I'll take that offline with you after the "
            "call, along with the detailed segment numbers you asked about.",
            "pleasantries only",
        ),
        ("Yes, we will.", "under 40 tokens with no number or period"),
        ("Thank you, and good evening everyone.", "no forward-looking keyword"),
        # Short but specific: still sent.
        ("Yes, we expect 20% margin next year.", None),
        ("We will commission the plant in Q3.", None),
        # Real outlook next to a hand-over: still sent.
        (
            "Thank you, Priya. We expect EBITDA margin to improve meaningfully over the next "
            "few quarters as the new line ramps up. I will now hand over to the moderator.",
            None,
        ),
        (
            "We remain committed to delivering on our FY27 targets and look forward to "
            "speaking with you next quarter.",
            None,
        ),
    ],
)
def test_strict_prefilter(text: str, reason: str | None) -> None:
    assert skip_reason(text, strict=True) == reason


def test_lenient_prefilter_matches_the_keyword_check() -> None:
    text = "I will now hand over to our CFO."
    assert skip_reason(text, strict=False) is None
    assert skip_reason("Thank you.", strict=False) == "no forward-looking keyword"


def test_plan_batches_respects_token_and_chunk_limits() -> None:
    chunks = [Chunk(text="x", token_count=n) for n in (900, 900, 900, 3000, 100, 100, 100)]
    batches = plan_batches(chunks, list(range(7)), max_tokens=2500, max_chunks=2)
    assert batches == [[0, 1], [2], [3], [4, 5], [6]]
    assert plan_batches(chunks, [4, 5, 6], max_tokens=2500, max_chunks=6) == [[4, 5, 6]]
    assert plan_batches(chunks, [], max_tokens=2500, max_chunks=6) == []


def test_plan_batches_respects_expected_output() -> None:
    dense = " ".join(f"We expect line {i} to add {10 * i} units in FY27." for i in range(1, 11))
    chunks = [Chunk(text=dense, token_count=150) for _ in range(3)]
    # 10 expected claims x 130 tokens each: one dense chunk per request under 2,500.
    assert plan_batches(
        chunks, [0, 1, 2], max_tokens=2500, max_chunks=6, max_output_tokens=2500
    ) == [[0], [1], [2]]
    assert plan_batches(chunks, [0, 1, 2], max_tokens=2500, max_chunks=6) == [[0, 1, 2]]
