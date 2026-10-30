import hashlib
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from tests.factories import TRANSCRIPT_PAGES, create_company, make_pdf, upload_pdf


@pytest.fixture
async def company(client: httpx.AsyncClient) -> dict[str, Any]:
    return await create_company(client)


@pytest.fixture
def transcript_pdf() -> bytes:
    return make_pdf(TRANSCRIPT_PAGES)


def _staging_files(settings: Settings) -> list[Path]:
    staging = settings.upload_dir / ".staging"
    return list(staging.iterdir()) if staging.exists() else []


async def test_upload_happy_path(
    client: httpx.AsyncClient, settings: Settings, company: dict[str, Any], transcript_pdf: bytes
) -> None:
    response = await upload_pdf(
        client, company["id"], transcript_pdf, period_label="q2 fy2026", filename="../call.pdf"
    )

    assert response.status_code == 201, response.text
    doc = response.json()
    sha = hashlib.sha256(transcript_pdf).hexdigest()
    assert doc["company_id"] == company["id"]
    assert doc["doc_type"] == "transcript"
    assert (doc["fiscal_year"], doc["fiscal_quarter"]) == (2026, 2)
    assert doc["period_label"] == "Q2FY26"
    assert doc["original_filename"] == "call.pdf"
    assert doc["file_hash"] == sha
    assert doc["page_count"] == 3
    assert doc["status"] == "parsed"
    assert doc["error_message"] is None

    stored = settings.upload_dir / company["id"] / f"{sha}.pdf"
    assert stored.read_bytes() == transcript_pdf
    assert _staging_files(settings) == []

    fetched = await client.get(f"/documents/{doc['id']}")
    assert fetched.status_code == 200
    # Transcripts are chunked and embedded in the background after the response.
    ignored = {"status", "updated_at"}
    assert {k: v for k, v in fetched.json().items() if k not in ignored} == {
        k: v for k, v in doc.items() if k not in ignored
    }
    assert fetched.json()["status"] == "embedded"


async def test_duplicate_upload_conflict(
    client: httpx.AsyncClient, settings: Settings, company: dict[str, Any], transcript_pdf: bytes
) -> None:
    first = await upload_pdf(client, company["id"], transcript_pdf)
    assert first.status_code == 201

    other = await create_company(client, name="Other", nse_symbol="OTHER", bse_code="500002")
    for company_id in (company["id"], other["id"]):
        response = await upload_pdf(client, company_id, transcript_pdf, period_label="Q3FY26")
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "duplicate_document"
        assert first.json()["id"] in error["message"]

    assert _staging_files(settings) == []
    assert not (settings.upload_dir / other["id"]).exists()
    listing = await client.get(f"/companies/{company['id']}/documents")
    assert len(listing.json()) == 1


async def test_non_pdf_rejected(
    client: httpx.AsyncClient, settings: Settings, company: dict[str, Any]
) -> None:
    response = await upload_pdf(
        client, company["id"], b"Not a PDF at all, just text.\n" * 100, filename="fake.pdf"
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "invalid_file_type"
    assert _staging_files(settings) == []
    assert not (settings.upload_dir / company["id"]).exists()


async def test_empty_file_rejected(client: httpx.AsyncClient, company: dict[str, Any]) -> None:
    response = await upload_pdf(client, company["id"], b"")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_file"


async def test_upload_too_large_rejected(
    client: httpx.AsyncClient, settings: Settings, company: dict[str, Any]
) -> None:
    settings.max_upload_mb = 1
    client_app: Any = client._transport.app  # type: ignore[attr-defined]
    client_app.state.storage.max_bytes = settings.max_upload_bytes

    payload = b"%PDF-1.7\n" + b"0" * (settings.max_upload_bytes + 10)
    response = await upload_pdf(client, company["id"], payload)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert _staging_files(settings) == []


async def test_corrupt_pdf_marked_failed(
    client: httpx.AsyncClient, company: dict[str, Any]
) -> None:
    response = await upload_pdf(client, company["id"], b"%PDF-1.7\nbroken body" * 10)
    assert response.status_code == 201
    doc = response.json()
    assert doc["status"] == "failed"
    assert doc["error_message"]
    assert doc["page_count"] is None


async def test_scanned_pdf_warning_still_parsed(
    client: httpx.AsyncClient, company: dict[str, Any]
) -> None:
    response = await upload_pdf(client, company["id"], make_pdf(["", "", ""]))
    assert response.status_code == 201
    doc = response.json()
    assert doc["status"] == "parsed"
    assert doc["page_count"] == 3
    assert doc["error_message"].startswith("Likely scanned PDF")


@pytest.mark.parametrize(
    ("doc_type", "label"),
    [("annual_report", "Q2FY26"), ("transcript", "FY26"), ("transcript", "H1FY26")],
)
async def test_invalid_period_rejected(
    client: httpx.AsyncClient,
    settings: Settings,
    company: dict[str, Any],
    transcript_pdf: bytes,
    doc_type: str,
    label: str,
) -> None:
    response = await upload_pdf(
        client, company["id"], transcript_pdf, doc_type=doc_type, period_label=label
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_period_label"
    assert _staging_files(settings) == []


async def test_invalid_doc_type_rejected(
    client: httpx.AsyncClient, company: dict[str, Any], transcript_pdf: bytes
) -> None:
    response = await upload_pdf(client, company["id"], transcript_pdf, doc_type="memo")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_upload_to_missing_company(client: httpx.AsyncClient, transcript_pdf: bytes) -> None:
    response = await upload_pdf(client, str(uuid.uuid4()), transcript_pdf)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "company_not_found"


async def test_list_documents_sorted_and_filtered(
    client: httpx.AsyncClient, company: dict[str, Any]
) -> None:
    uploads = [
        ("transcript", "Q4FY25"),
        ("transcript", "Q2FY26"),
        ("annual_report", "FY25"),
        ("results", "Q1FY26"),
        ("transcript", "Q1FY26"),
    ]
    for i, (doc_type, label) in enumerate(uploads):
        pdf = make_pdf([f"Document number {i} with enough text to count as a real page."])
        response = await upload_pdf(
            client, company["id"], pdf, doc_type=doc_type, period_label=label
        )
        assert response.status_code == 201, response.text

    url = f"/companies/{company['id']}/documents"
    desc = (await client.get(url)).json()
    assert [d["period_label"] for d in desc] == ["Q2FY26", "Q1FY26", "Q1FY26", "FY25", "Q4FY25"]

    asc = (await client.get(url, params={"order": "asc"})).json()
    assert [d["period_label"] for d in asc] == ["Q4FY25", "FY25", "Q1FY26", "Q1FY26", "Q2FY26"]

    transcripts = (await client.get(url, params={"doc_type": "transcript"})).json()
    assert [d["period_label"] for d in transcripts] == ["Q2FY26", "Q1FY26", "Q4FY25"]
    assert {d["doc_type"] for d in transcripts} == {"transcript"}

    missing = await client.get(f"/companies/{uuid.uuid4()}/documents")
    assert missing.status_code == 404


async def test_page_retrieval(
    client: httpx.AsyncClient, company: dict[str, Any], transcript_pdf: bytes
) -> None:
    doc = (await upload_pdf(client, company["id"], transcript_pdf)).json()
    url = f"/documents/{doc['id']}/pages"

    everything = (await client.get(url)).json()
    assert everything["page_count"] == 3
    assert (everything["start"], everything["end"]) == (1, 3)
    assert [p["page_number"] for p in everything["pages"]] == [1, 2, 3]
    page2 = everything["pages"][1]
    # Speaker names stay on their own lines.
    assert page2["text"].splitlines()[0] == "Moderator:"
    assert "\nRahul Mehta:\n" in page2["text"]
    assert page2["char_count"] == len(page2["text"])

    middle = (await client.get(url, params={"start": 2, "end": 2})).json()
    assert [p["page_number"] for p in middle["pages"]] == [2]

    tail = (await client.get(url, params={"start": 3, "end": 10})).json()
    assert [p["page_number"] for p in tail["pages"]] == [3]
    assert tail["end"] == 3

    beyond = (await client.get(url, params={"start": 5})).json()
    assert beyond["pages"] == []


async def test_page_retrieval_validation(
    client: httpx.AsyncClient, company: dict[str, Any], transcript_pdf: bytes
) -> None:
    doc = (await upload_pdf(client, company["id"], transcript_pdf)).json()
    url = f"/documents/{doc['id']}/pages"

    assert (await client.get(url, params={"start": 3, "end": 2})).status_code == 422
    assert (await client.get(url, params={"start": 1, "end": 51})).status_code == 422
    assert (await client.get(url, params={"start": 0})).status_code == 422
    assert (await client.get(f"/documents/{uuid.uuid4()}/pages")).status_code == 404


async def test_delete_removes_file_and_rows(
    client: httpx.AsyncClient, settings: Settings, company: dict[str, Any], transcript_pdf: bytes
) -> None:
    doc = (await upload_pdf(client, company["id"], transcript_pdf)).json()
    stored = settings.upload_dir / company["id"] / f"{doc['file_hash']}.pdf"
    assert stored.exists()

    response = await client.delete(f"/documents/{doc['id']}")
    assert response.status_code == 204

    assert not stored.exists()
    assert (await client.get(f"/documents/{doc['id']}")).status_code == 404
    assert (await client.get(f"/documents/{doc['id']}/pages")).status_code == 404
    assert (await client.get(f"/companies/{company['id']}/documents")).json() == []
    assert (await client.delete(f"/documents/{doc['id']}")).status_code == 404

    # The same file can be uploaded again once deleted.
    again = await upload_pdf(client, company["id"], transcript_pdf)
    assert again.status_code == 201
