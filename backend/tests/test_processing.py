"""Chunking + embedding pipeline and the process/chunks/search endpoints (DB-backed)."""

import textwrap
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select

from app.models import Chunk
from app.services import ingestion
from app.services.embeddings import EmbeddingError, EmbeddingPurpose, FakeEmbeddingProvider
from app.services.ingestion import IngestionError, chunk_document, process_document
from tests.concall_fixture import ANALYSTS, LONG_ANSWER_FIRST, build_concall_pdf
from tests.factories import TRANSCRIPT_PAGES, create_company, make_pdf, upload_pdf

RESULTS_PAGES = [
    textwrap.fill(
        " ".join(
            f"Segment {s} revenue for the quarter was Rs. {100 + 7 * s} crore with EBITDA of "
            f"Rs. {20 + s} crore and order inflows of Rs. {300 + 11 * s} crore."
            for s in range(page * 12, page * 12 + 12)
        ),
        width=95,
    )
    for page in range(4)
]


class FlakyProvider(FakeEmbeddingProvider):
    """Small batches; fails on call number ``fail_on_call``."""

    max_batch_size = 4

    def __init__(self, fail_on_call: int) -> None:
        super().__init__()
        self.fail_on_call = fail_on_call

    async def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> list[list[float]]:
        if len(self.calls) + 1 == self.fail_on_call:
            self.calls.append((purpose, list(texts)))
            raise EmbeddingError("Embedding request failed after 5 attempt(s): simulated 429")
        return await super().embed(texts, purpose)


class BrokenProvider(FakeEmbeddingProvider):
    async def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> list[list[float]]:
        raise EmbeddingError("Gemini API error 503 UNAVAILABLE: simulated")


@pytest.fixture(scope="module")
def concall_pdf() -> bytes:
    return build_concall_pdf()


@pytest.fixture
async def company(client: httpx.AsyncClient) -> dict[str, Any]:
    return await create_company(client)


@pytest.fixture
def fake(app: FastAPI) -> FakeEmbeddingProvider:
    provider: FakeEmbeddingProvider = app.state.embedder
    return provider


async def _upload(
    client: httpx.AsyncClient, company_id: str, pdf: bytes, **kw: Any
) -> dict[str, Any]:
    response = await upload_pdf(client, company_id, pdf, **kw)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _chunks(client: httpx.AsyncClient, document_id: str, **params: Any) -> dict[str, Any]:
    response = await client.get(f"/documents/{document_id}/chunks", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _status(client: httpx.AsyncClient, document_id: str) -> str:
    status: str = (await client.get(f"/documents/{document_id}")).json()["status"]
    return status


async def _embedded_count(app: FastAPI, document_id: str) -> int:
    async with app.state.sessionmaker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.document_id == uuid.UUID(document_id), Chunk.embedding.is_not(None))
        )
    return int(count or 0)


# ------------------------------------------------------------------ pipeline


async def test_transcript_upload_is_processed_in_background(
    client: httpx.AsyncClient,
    company: dict[str, Any],
    fake: FakeEmbeddingProvider,
    concall_pdf: bytes,
) -> None:
    doc = await _upload(client, company["id"], concall_pdf)
    assert doc["status"] == "parsed"  # the upload response does not wait for processing
    assert doc["page_count"] >= 5

    assert await _status(client, doc["id"]) == "embedded"
    listing = await _chunks(client, doc["id"], limit=200)
    chunks = listing["chunks"]
    assert listing["total"] == len(chunks) >= 15
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert all(c["has_embedding"] for c in chunks)
    assert {c["section"] for c in chunks} == {"other", "prepared_remarks", "qna"}

    [(purpose, texts)] = fake.calls
    assert purpose == "document"
    assert len(texts) == len(chunks)
    long_answer = next(c for c in chunks if c["text"].startswith(LONG_ANSWER_FIRST[:40]))
    assert texts[long_answer["chunk_index"]].startswith(
        "[Acme Industries Ltd | Q2FY26 | Rajesh Kumar (management) | Q&A]\n"
    )
    # Stored text has no prefix.
    assert not any(c["text"].startswith("[") for c in chunks)


async def test_chunks_endpoint_filters_and_pages(
    client: httpx.AsyncClient, company: dict[str, Any], concall_pdf: bytes
) -> None:
    doc = await _upload(client, company["id"], concall_pdf)

    qna = await _chunks(client, doc["id"], section="qna")
    assert qna["total"] > 0 and {c["section"] for c in qna["chunks"]} == {"qna"}

    analysts = await _chunks(client, doc["id"], speaker_role="analyst")
    assert {c["speaker"] for c in analysts["chunks"]} == set(ANALYSTS)

    management_qna = await _chunks(client, doc["id"], section="qna", speaker_role="management")
    assert {(c["section"], c["speaker_role"]) for c in management_qna["chunks"]} == {
        ("qna", "management")
    }

    everything = await _chunks(client, doc["id"], limit=200)
    page = await _chunks(client, doc["id"], limit=3, offset=2)
    assert page["total"] == everything["total"]
    assert [c["id"] for c in page["chunks"]] == [c["id"] for c in everything["chunks"][2:5]]

    assert (await client.get(f"/documents/{uuid.uuid4()}/chunks")).status_code == 404
    bad = await client.get(f"/documents/{doc['id']}/chunks", params={"section": "intro"})
    assert bad.status_code == 422


async def test_non_transcript_processed_on_request(
    client: httpx.AsyncClient, company: dict[str, Any], fake: FakeEmbeddingProvider
) -> None:
    doc = await _upload(
        client, company["id"], make_pdf(RESULTS_PAGES), doc_type="results", period_label="Q2FY26"
    )
    assert await _status(client, doc["id"]) == "parsed"  # not auto-processed
    assert fake.calls == []

    response = await client.post(f"/documents/{doc['id']}/process")
    assert response.status_code == 202
    assert response.json()["status"] == "parsed"  # status at the time of the request

    assert await _status(client, doc["id"]) == "embedded"
    chunks = (await _chunks(client, doc["id"]))["chunks"]
    assert len(chunks) >= 2
    assert {(c["speaker"], c["speaker_role"], c["section"]) for c in chunks} == {
        (None, "unknown", "other")
    }
    assert chunks[0]["page_start"] == 1 and chunks[-1]["page_end"] == 4
    assert all(c["token_count"] <= 500 for c in chunks)
    assert fake.calls[0][1][0].startswith("[Acme Industries Ltd | Q2FY26 | Results | Other]\n")


async def test_process_requires_parsed_document(
    client: httpx.AsyncClient, company: dict[str, Any]
) -> None:
    doc = await _upload(client, company["id"], b"%PDF-1.7\nbroken body" * 10)
    assert doc["status"] == "failed"

    response = await client.post(f"/documents/{doc['id']}/process")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "document_not_parsed"
    assert (await client.post(f"/documents/{uuid.uuid4()}/process")).status_code == 404


async def test_scanned_transcript_fails_with_reason(
    client: httpx.AsyncClient, company: dict[str, Any]
) -> None:
    doc = await _upload(client, company["id"], make_pdf(["", "", ""]))
    body = (await client.get(f"/documents/{doc['id']}")).json()
    assert body["status"] == "failed"
    assert "No text to chunk" in body["error_message"]


async def test_process_conflict_while_running(
    client: httpx.AsyncClient, app: FastAPI, company: dict[str, Any]
) -> None:
    doc = await _upload(client, company["id"], make_pdf(RESULTS_PAGES), doc_type="results")
    tracker = app.state.processing
    assert tracker.try_start(uuid.UUID(doc["id"]))
    try:
        response = await client.post(f"/documents/{doc['id']}/process")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "processing_in_progress"
    finally:
        tracker.finish(uuid.UUID(doc["id"]))
    assert (await client.post(f"/documents/{doc['id']}/process")).status_code == 202
    assert not tracker.is_active(uuid.UUID(doc["id"]))


async def test_rechunking_is_idempotent(
    client: httpx.AsyncClient, app: FastAPI, company: dict[str, Any], concall_pdf: bytes
) -> None:
    doc = await _upload(client, company["id"], concall_pdf)
    before = (await _chunks(client, doc["id"], limit=200))["chunks"]

    def shape(chunks: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        keys = ("chunk_index", "text", "page_start", "page_end", "speaker", "section")
        return [tuple(c[k] for k in keys) for c in chunks]

    async with app.state.sessionmaker() as session:
        count = await chunk_document(session, uuid.UUID(doc["id"]))
        assert await chunk_document(session, uuid.UUID(doc["id"])) == count == len(before)
    assert await _status(client, doc["id"]) == "chunked"
    assert await _embedded_count(app, doc["id"]) == 0  # embeddings were dropped with the chunks

    response = await client.post(f"/documents/{doc['id']}/process", params={"rechunk": "true"})
    assert response.status_code == 202
    after = (await _chunks(client, doc["id"], limit=200))["chunks"]

    assert shape(after) == shape(before)
    assert not {c["id"] for c in before} & {c["id"] for c in after}
    assert await _status(client, doc["id"]) == "embedded"
    assert all(c["has_embedding"] for c in after)


async def test_chunking_requires_parsed_status(
    client: httpx.AsyncClient, app: FastAPI, company: dict[str, Any]
) -> None:
    doc = await _upload(client, company["id"], b"%PDF-1.7\nbroken body" * 10)
    async with app.state.sessionmaker() as session:
        with pytest.raises(IngestionError, match="must be parsed"):
            await chunk_document(session, uuid.UUID(doc["id"]))


async def test_embedding_resumes_after_partial_failure(
    client: httpx.AsyncClient, app: FastAPI, company: dict[str, Any], concall_pdf: bytes
) -> None:
    doc = await _upload(client, company["id"], concall_pdf)
    document_id = uuid.UUID(doc["id"])
    async with app.state.sessionmaker() as session:
        total = await chunk_document(session, document_id)  # back to "chunked", no vectors

    flaky = FlakyProvider(fail_on_call=2)
    await process_document(app.state.sessionmaker, flaky, document_id)

    failed = (await client.get(f"/documents/{doc['id']}")).json()
    assert failed["status"] == "failed"
    assert "simulated 429" in failed["error_message"]
    assert await _embedded_count(app, doc["id"]) == 4  # the first batch was committed

    async with app.state.sessionmaker() as session:
        first_batch = {
            c.id: list(c.embedding)
            for c in await session.scalars(
                select(Chunk).where(Chunk.document_id == document_id, Chunk.embedding.is_not(None))
            )
        }

    resumed = FakeEmbeddingProvider()
    await process_document(app.state.sessionmaker, resumed, document_id)

    body = (await client.get(f"/documents/{doc['id']}")).json()
    assert body["status"] == "embedded"
    assert body["error_message"] is None
    assert sum(len(texts) for _, texts in resumed.calls) == total - 4  # only the missing ones
    assert await _embedded_count(app, doc["id"]) == total
    async with app.state.sessionmaker() as session:
        for chunk_id, vector in first_batch.items():
            chunk = await session.get(Chunk, chunk_id)
            assert chunk is not None and list(chunk.embedding) == vector  # kept, not redone


def test_error_messages_never_contain_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "AIzaSy-test-secret-value"

    class _Settings:
        gemini_api_key = SecretStr(key)

    monkeypatch.setattr(ingestion, "get_settings", lambda: _Settings())
    message = ingestion.safe_error_message(RuntimeError(f"bad request with key={key}"))
    assert key not in message
    assert message == "RuntimeError: bad request with key=[redacted]"
    assert len(ingestion.safe_error_message(RuntimeError("x" * 5000))) == 1000


# -------------------------------------------------------------------- search


@pytest.fixture
async def search_setup(
    client: httpx.AsyncClient, company: dict[str, Any], concall_pdf: bytes
) -> dict[str, Any]:
    q2 = await _upload(client, company["id"], concall_pdf, period_label="Q2FY26")
    q1 = await _upload(client, company["id"], make_pdf(TRANSCRIPT_PAGES), period_label="Q1FY25")
    other = await create_company(client, name="Other Co", nse_symbol="OTHER", bse_code="500002")
    other_doc = await _upload(
        client, other["id"], make_pdf(RESULTS_PAGES), doc_type="results", period_label="Q2FY26"
    )
    assert (await client.post(f"/documents/{other_doc['id']}/process")).status_code == 202
    for doc in (q2, q1, other_doc):
        assert await _status(client, doc["id"]) == "embedded"
    return {"company": company, "q2": q2, "q1": q1, "other_doc": other_doc}


async def _search(client: httpx.AsyncClient, company_id: str, **body: Any) -> httpx.Response:
    return await client.post(f"/companies/{company_id}/search", json=body)


async def test_search_returns_ranked_chunks(
    client: httpx.AsyncClient, search_setup: dict[str, Any], fake: FakeEmbeddingProvider
) -> None:
    company_id = search_setup["company"]["id"]
    response = await _search(client, company_id, query="EBITDA margin guidance for FY27")

    assert response.status_code == 200, response.text
    body = response.json()
    results = body["results"]
    assert body["query"] == "EBITDA margin guidance for FY27"
    assert 1 <= len(results) <= 8
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    assert all(-1.0 <= s <= 1.0 for s in scores)
    assert "EBITDA margin" in results[0]["text"]
    own_docs = {search_setup["q2"]["id"], search_setup["q1"]["id"]}
    assert {r["document_id"] for r in results} <= own_docs  # never another company's chunks
    assert set(results[0]) == {
        "chunk_id",
        "document_id",
        "score",
        "text",
        "speaker",
        "speaker_role",
        "section",
        "period_label",
        "page_start",
        "page_end",
        "thread_id",
    }
    assert results[0]["thread_id"] is None  # no claims extracted in this setup
    assert fake.calls[-1] == ("query", ["EBITDA margin guidance for FY27"])


async def test_search_filters(client: httpx.AsyncClient, search_setup: dict[str, Any]) -> None:
    company_id = search_setup["company"]["id"]
    query = "revenue growth guidance"

    async def results(**filters: Any) -> list[dict[str, Any]]:
        response = await _search(client, company_id, query=query, top_k=30, **filters)
        assert response.status_code == 200, response.text
        found: list[dict[str, Any]] = response.json()["results"]
        assert found, filters
        return found

    assert {r["section"] for r in await results(section="qna")} == {"qna"}
    assert {r["speaker_role"] for r in await results(speaker_role="management")} == {"management"}
    assert {
        (r["section"], r["speaker_role"])
        for r in await results(section="prepared_remarks", speaker_role="management")
    } == {("prepared_remarks", "management")}
    analysts = await results(speaker_role="analyst")
    assert {r["speaker"] for r in analysts} <= set(ANALYSTS)

    assert {r["period_label"] for r in await results(from_period="Q1FY25", to_period="Q1FY25")} == {
        "Q1FY25"
    }
    assert {r["period_label"] for r in await results(from_period="Q2FY26")} == {"Q2FY26"}
    assert {r["period_label"] for r in await results(to_period="Q4FY25")} == {"Q1FY25"}
    assert {r["period_label"] for r in await results(from_period="FY26", to_period="FY26")} == {
        "Q2FY26"
    }
    assert {r["period_label"] for r in await results()} == {"Q1FY25", "Q2FY26"}

    empty = await _search(client, company_id, query=query, from_period="Q3FY26", to_period="Q4FY26")
    assert empty.json()["results"] == []


async def test_search_top_k(client: httpx.AsyncClient, search_setup: dict[str, Any]) -> None:
    company_id = search_setup["company"]["id"]
    response = await _search(client, company_id, query="capex", top_k=3)
    assert len(response.json()["results"]) == 3


@pytest.mark.parametrize(
    "body",
    [
        {"query": "x", "top_k": 31},
        {"query": "x", "top_k": 0},
        {"query": "   "},
        {"query": "x", "section": "intro"},
        {"query": "x", "speaker_role": "ceo"},
        {"query": "x", "from_period": "H1FY26"},
        {"query": "x", "from_period": "Q3FY26", "to_period": "Q1FY26"},
    ],
)
async def test_search_validation(
    client: httpx.AsyncClient, company: dict[str, Any], body: dict[str, Any]
) -> None:
    response = await client.post(f"/companies/{company['id']}/search", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_search_unknown_company(client: httpx.AsyncClient) -> None:
    response = await _search(client, str(uuid.uuid4()), query="growth")
    assert response.status_code == 404


async def test_search_without_embeddings_is_empty(
    client: httpx.AsyncClient, company: dict[str, Any]
) -> None:
    response = await _search(client, company["id"], query="growth")
    assert response.status_code == 200
    assert response.json()["results"] == []


async def test_search_embedding_failure_is_502(
    client: httpx.AsyncClient, app: FastAPI, company: dict[str, Any]
) -> None:
    app.state.embedder = BrokenProvider()
    response = await _search(client, company["id"], query="growth")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "embedding_failed"
