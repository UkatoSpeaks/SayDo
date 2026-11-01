"""Run the full pipeline on a real transcript and print a quality report.

    uv run python -m scripts.check_transcript samples/TATAMOTORS_Q2FY26.pdf [--name "Tata Motors"]

The file name must be SYMBOL_PERIOD.pdf. Uses the dev database (DATABASE_URL) and,
unless --fake is passed, the real Gemini (embeddings) and Groq (extraction) APIs.
Steps: create (or reuse) the company, upload, chunk + embed, extract, then report
speaker stats, where Q&A starts, leftover header/footer lines, sample chunks,
extraction stats and sample claims. Re-running reuses the uploaded document and
resumes extraction (e.g. after the daily quota paused it).
"""

import argparse
import asyncio
import re
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import defer

from app.core.config import BACKEND_DIR, Settings
from app.main import create_app
from app.models import Chunk, ChunkSection, Claim, Company, Document, SpeakerRole
from app.services.transcript_cleaner import is_page_number

_FILENAME_RE = re.compile(r"^(?P<symbol>[A-Za-z0-9&\-]+)_(?P<period>[^_]+)\.pdf$", re.IGNORECASE)
_MODERATOR_PHRASES = re.compile(
    r"next question (?:is|comes) from|the line of|please go ahead|ladies and gentlemen"
    r"|that concludes|you may now disconnect|press star and one",
    re.IGNORECASE,
)
_TITLE_WORDS = re.compile(
    r"\b(?:cfo|ceo|md|chairman|director|president|officer|head|founder|cmd|vp)\b", re.IGNORECASE
)
_HEADER_HINTS = re.compile(
    r"earnings (?:conference )?call|conference call|limited$|ltd\.?$|^page\b|transcript"
    r"|^www\.|@|cin[:\s]|registered office",
    re.IGNORECASE,
)


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


async def _ensure_company(client: httpx.AsyncClient, symbol: str, name: str) -> dict[str, Any]:
    companies = (await client.get("/companies", params={"limit": 500})).json()
    for company in companies:
        if company["nse_symbol"] == symbol.upper():
            return dict(company)
    response = await client.post("/companies", json={"name": name, "nse_symbol": symbol})
    response.raise_for_status()
    return dict(response.json())


async def _ensure_document(
    client: httpx.AsyncClient, company_id: str, filename: str, content: bytes, period: str
) -> dict[str, Any]:
    response = await client.post(
        f"/companies/{company_id}/documents",
        files={"file": (filename, content, "application/pdf")},
        data={"doc_type": "transcript", "period_label": period},
    )
    if response.status_code == 409 and response.json()["error"]["code"] == "duplicate_document":
        match = re.search(r"document ([0-9a-f-]{36})", response.json()["error"]["message"])
        assert match, response.text
        print(f"Already uploaded; reusing document {match.group(1)}")
        document = (await client.get(f"/documents/{match.group(1)}")).json()
        if document["status"] in {"parsed", "chunked", "failed"}:
            await client.post(f"/documents/{document['id']}/process")
            document = (await client.get(f"/documents/{document['id']}")).json()
        return dict(document)
    response.raise_for_status()
    # Processing runs as a background task, which has finished when the call returns.
    return dict((await client.get(f"/documents/{response.json()['id']}")).json())


def _section(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * (70 - len(title))}")


def speaker_flags(chunks: list[Chunk]) -> list[str]:
    """Chunks whose speaker, role or section looks wrong."""
    flags: list[str] = []
    for c in chunks:
        first = _first_line(c.text)
        if c.speaker_role is SpeakerRole.UNKNOWN and c.speaker and _TITLE_WORDS.search(c.speaker):
            flags.append(
                f"chunk {c.chunk_index}: '{c.speaker}' looks like management but is unknown"
            )
        if c.speaker_role is not SpeakerRole.MODERATOR and _MODERATOR_PHRASES.search(c.text):
            hit = _MODERATOR_PHRASES.search(c.text)
            assert hit
            snippet = c.text[max(0, hit.start() - 60) : hit.end() + 40].replace("\n", " ")
            flags.append(
                f"chunk {c.chunk_index} ({c.speaker}, {c.speaker_role.value}) contains moderator "
                f"text: ...{snippet}..."
            )
        if c.speaker is None and c.section is not ChunkSection.OTHER:
            flags.append(
                f"chunk {c.chunk_index}: no speaker inside {c.section.value}: {first[:80]}"
            )
    unknown = {c.speaker for c in chunks if c.speaker_role is SpeakerRole.UNKNOWN and c.speaker}
    if unknown:
        flags.append(f"speakers with role unknown: {sorted(unknown)}")
    return flags


def leftover_lines(chunks: list[Chunk]) -> list[tuple[str, int]]:
    """Lines inside chunks that look like page headers/footers, most frequent first."""
    line_counts: Counter[str] = Counter()
    for c in chunks:
        for line in {line.strip() for line in c.text.splitlines() if line.strip()}:
            line_counts[line] += 1
    suspects = [
        (line, n)
        for line, n in line_counts.items()
        if is_page_number(line)
        or (n >= 3 and len(line) < 120)
        or (_HEADER_HINTS.search(line) and len(line) < 80)
    ]
    return sorted(suspects, key=lambda x: -x[1])


def transcript_warnings(chunks: list[Chunk]) -> list[str]:
    """The checks above as one list of warnings (empty when the transcript looks fine)."""
    if not chunks:
        return ["no chunks: the PDF has no extractable text (scanned?)"]
    warnings: list[str] = []
    roles = Counter(c.speaker_role for c in chunks)
    if roles[SpeakerRole.MANAGEMENT] == 0:
        warnings.append("no management chunks: speakers were not recognised, nothing to extract")
    if roles[SpeakerRole.ANALYST] == 0:
        warnings.append("no analyst chunks: analysts were not recognised")
    if not any(c.section is ChunkSection.QNA for c in chunks):
        warnings.append("no Q&A section detected")
    warnings.extend(speaker_flags(chunks))
    warnings.extend(
        f"leftover header/footer line ({n}x): {line[:110]!r}"
        for line, n in leftover_lines(chunks)[:10]
    )
    return warnings


def report_chunks(chunks: list[Chunk]) -> None:
    _section("Speakers")
    counts = Counter((c.speaker, c.speaker_role.value) for c in chunks)
    for (speaker, role), n in sorted(counts.items(), key=lambda kv: (kv[0][1], -kv[1])):
        print(f"  {speaker or '(no label)':40} {role:11} {n:3} chunk(s)")

    flags = speaker_flags(chunks)
    print("\n  Possible misclassifications:" if flags else "\n  No obvious misclassifications.")
    for flag in flags:
        print(f"  - {flag}")

    _section("Q&A start")
    first_qna = next((c for c in chunks if c.section is ChunkSection.QNA), None)
    if first_qna is None:
        print("  No Q&A section detected!")
    else:
        first_line = _first_line(first_qna.text)[:150]
        print(
            f"  chunk {first_qna.chunk_index}, page {first_qna.page_start}, "
            f"{first_qna.speaker} ({first_qna.speaker_role.value}): {first_line}"
        )

    _section("Leftover header/footer lines inside chunks")
    suspects = leftover_lines(chunks)
    if not suspects:
        print("  None found.")
    for line, n in suspects[:20]:
        print(f"  {n:3}x  {line[:110]!r}")

    _section("Sample chunks")
    picks = [
        next(
            (
                c
                for c in chunks
                if c.section is ChunkSection.PREPARED_REMARKS
                and c.speaker_role is SpeakerRole.MANAGEMENT
            ),
            None,
        ),
        next((c for c in chunks if c.speaker_role is SpeakerRole.ANALYST), None),
        next(
            (
                c
                for c in chunks
                if c.section is ChunkSection.QNA and c.speaker_role is SpeakerRole.MANAGEMENT
            ),
            None,
        ),
    ]
    for pick in picks:
        if pick is not None:
            text = " ".join(pick.text.split())[:200]
            print(
                f"  [{pick.chunk_index}] {pick.speaker} | {pick.speaker_role.value} | "
                f"{pick.section.value} | "
                f"p{pick.page_start}-{pick.page_end} | {pick.token_count} tok\n      {text}"
            )


def _target(claim: Claim) -> str:
    if claim.target_low is None:
        return "-"
    low = format(claim.target_low, "f")
    high = format(claim.target_high, "f") if claim.target_high is not None else low
    return low if low == high else f"{low}-{high}"


def report_claims(runs: list[dict[str, Any]], claims: list[Claim]) -> None:
    _section("Extraction runs (newest first)")
    for run in runs:
        print(
            f"  {run['status']:12} model={run['model']} prompt={run['prompt_version']} "
            f"total={run['chunks_total']} skipped={run['chunks_skipped']} "
            f"processed={run['chunks_processed']} created={run['claims_created']} "
            f"rejected={run['claims_rejected']} tokens={run['tokens_in']}+{run['tokens_out']}"
        )
        if run["error_message"]:
            print(f"      error: {run['error_message']}")

    _section(f"Claims ({len(claims)})")
    for claim in claims:
        period = (
            f"FY{claim.target_fiscal_year % 100:02d}" if claim.target_fiscal_year else "none"
        ) + (f" Q{claim.target_fiscal_quarter}" if claim.target_fiscal_quarter else "")
        span = " (range)" if claim.timeframe_is_range else ""
        print(
            f"  p{claim.page_number} {claim.metric_normalized} [{claim.claim_type.value}, "
            f"{claim.direction.value}] target={_target(claim)} {claim.unit or ''} "
            f"| {claim.timeframe_raw!r} -> {period}{span} "
            f"| conf={claim.extraction_confidence:.2f} x{claim.mention_count} | {claim.speaker}"
            f"\n      {claim.quote}"
        )
        if claim.validation_notes:
            print(f"      notes: {claim.validation_notes}")


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--name", help="Company name used in the prompt (default: the symbol)")
    parser.add_argument(
        "--fake", action="store_true", help="Fake embeddings and LLM (no API calls)"
    )
    parser.add_argument("--reextract", action="store_true", help="Delete claims and extract again")
    parser.add_argument(
        "--cleanup", action="store_true", help="Delete the document (and company) afterwards"
    )
    args = parser.parse_args()

    pdf: Path = args.pdf if args.pdf.is_absolute() else (BACKEND_DIR / args.pdf)
    match = _FILENAME_RE.match(pdf.name)
    if not pdf.is_file() or match is None:
        print(f"Expected an existing SYMBOL_PERIOD.pdf, got {pdf}", file=sys.stderr)
        return 2
    return asyncio.run(main(args, pdf.name, pdf.read_bytes(), match["symbol"], match["period"]))


async def main(
    args: argparse.Namespace, filename: str, content: bytes, symbol: str, period: str
) -> int:
    symbol, period = symbol.upper(), period.upper()

    overrides: dict[str, Any] = {"auto_extract": False}
    if args.fake:
        overrides |= {"embedding_provider": "fake", "llm_provider": "fake"}
    settings = Settings().model_copy(update=overrides)
    if not args.fake and (settings.groq_api_key is None or settings.gemini_api_key is None):
        print("GROQ_API_KEY and GEMINI_API_KEY must be set in backend/.env", file=sys.stderr)
        return 2
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://local", timeout=None
    ) as client:
        company = await _ensure_company(client, symbol, args.name or symbol)
        document = await _ensure_document(client, company["id"], filename, content, period)
        print(
            f"Company {company['name']} ({company['id']}), document {document['id']} "
            f"{document['period_label']}: {document['page_count']} pages, "
            f"status {document['status']}"
        )
        if document["status"] not in {"embedded", "extracted"}:
            print(f"Processing did not finish: {document['error_message']}", file=sys.stderr)
            return 1

        if document["status"] == "embedded" or args.reextract:
            response = await client.post(
                f"/documents/{document['id']}/extract",
                params={"reextract": str(args.reextract).lower()},
            )
            response.raise_for_status()
        runs = (await client.get(f"/documents/{document['id']}/extraction-runs")).json()

        async with app.state.sessionmaker() as session:
            document_id = uuid.UUID(document["id"])
            chunks = list(
                await session.scalars(
                    select(Chunk)
                    .options(defer(Chunk.embedding))
                    .where(Chunk.document_id == document_id)
                    .order_by(Chunk.chunk_index)
                )
            )
            claims = list(
                await session.scalars(
                    select(Claim)
                    .where(Claim.document_id == document_id)
                    .order_by(Claim.page_number, Claim.created_at)
                )
            )
            doc = await session.get(Document, document_id)
            assert doc is not None
        print(f"Document status: {doc.status.value}; {len(chunks)} chunks")
        report_chunks(chunks)
        report_claims(runs, claims)

        if args.cleanup:
            await client.delete(f"/documents/{document['id']}")
            remaining = (await client.get(f"/companies/{company['id']}/documents")).json()
            if not remaining:
                async with app.state.sessionmaker() as session:
                    row = await session.get(Company, uuid.UUID(company["id"]))
                    if row is not None:
                        await session.delete(row)
                        await session.commit()
            print("\nCleaned up.")
    await app.state.engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(cli())
