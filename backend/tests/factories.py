"""Test data builders."""

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pymupdf

from app.services.pdf_parser import extract_pdf


def make_pdf(pages: list[str]) -> bytes:
    """Build a PDF with one page per string (empty string -> blank page)."""
    doc: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    for content in pages:
        page = doc.new_page()
        if content:
            page.insert_text((72, 72), content, fontsize=11)
    data: bytes = doc.tobytes()
    doc.close()
    return data


TRANSCRIPT_PAGES = [
    "Acme Industries Limited\nQ2 FY26 Earnings Conference Call\nOctober 28, 2025\n"
    "Management:\nMr. Rahul Mehta - Chief Financial Officer",
    "Moderator:\nLadies and gentlemen, good day and welcome to the call.\n"
    "Rahul Mehta:\nThank you. We expect revenue growth of 15-18% in FY27.",
    "Analyst - Priya Shah:\nWhat is the EBITDA margin guidance?\n"
    "Rahul Mehta:\nWe aim to reach   20% EBITDA margin   by Q4 FY27.",
]


async def create_company(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Acme Industries Ltd",
        "nse_symbol": "ACME",
        "bse_code": "500001",
        "sector": "Industrials",
    }
    payload.update(overrides)
    response = await client.post("/companies", json=payload)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def upload_pdf(
    client: httpx.AsyncClient,
    company_id: str,
    content: bytes,
    *,
    doc_type: str = "transcript",
    period_label: str = "Q2FY26",
    filename: str = "transcript.pdf",
) -> httpx.Response:
    return await client.post(
        f"/companies/{company_id}/documents",
        files={"file": (filename, content, "application/pdf")},
        data={"doc_type": doc_type, "period_label": period_label},
    )


def extracted_pages(pdf: bytes, tmp_dir: Path) -> list[str]:
    """Page texts exactly as the upload pipeline extracts them."""
    path = tmp_dir / f"{hashlib.sha256(pdf).hexdigest()[:12]}.pdf"
    path.write_bytes(pdf)
    return [page.text for page in extract_pdf(path).pages]
