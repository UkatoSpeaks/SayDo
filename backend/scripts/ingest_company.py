"""Add a real company end to end: upload its transcripts, process, extract, then verify.

    uv run python -m scripts.ingest_company --symbol INFY --name "Infosys Ltd" \\
        --sector "IT services" --dir samples/INFY [--actuals samples/INFY/actuals.csv]

Steps (every step is resumable; re-run the same command to continue):

1. Create the company if no company has this NSE symbol.
2. Upload every ``SYMBOL_Q<n>FY<yy>.pdf`` directly in ``--dir`` as a transcript for that
   quarter. Files already uploaded (same SHA-256) are reused; files that aren't PDFs or are
   too large are skipped.
3. Oldest quarter first: chunk + embed (Gemini), then extract claims (Groq). Before a
   document's first extraction the transcript checks (speakers, Q&A start, leftover
   headers) are printed. When Groq's daily quota runs out the run pauses and the command
   stops; the next run continues from the first chunk not yet handled.
4. Once every document is extracted: write ``<dir>/actuals_todo.csv`` (the actuals
   template with an empty value column) listing each metric/unit/period needed to verify
   numeric and directional promises whose target period has ended.
5. If ``--actuals`` is given or ``<dir>/actuals.csv`` exists: import it (rows with an
   empty value are skipped), run link + verify and print the scorecard.

Uses the dev database (DATABASE_URL) and refuses to run with fake providers unless
``--allow-fake`` is passed (tests only).

``--reextract`` extracts again every transcript whose last extraction used an older
prompt version (its claims are replaced); paused re-extractions resume on the next run.

Exit codes: 0 done, 1 a step failed, 2 bad arguments/config, 3 paused (Groq daily limit).
"""

import argparse
import asyncio
import csv
import hashlib
import io
import logging
import re
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer

from app.core.config import BACKEND_DIR, Settings
from app.core.db import create_engine, create_sessionmaker
from app.core.errors import AppError
from app.models import (
    Chunk,
    ClaimType,
    Company,
    DocType,
    Document,
    DocumentStatus,
    ExtractionRun,
    ExtractionRunStatus,
    SpeakerRole,
    VerificationRunStatus,
)
from app.services.actuals_import import COLUMNS, OPTIONAL_COLUMNS, import_actuals
from app.services.claim_rules import reclassify_company_claims
from app.services.deterministic_check import baseline_period
from app.services.documents import ingest_document
from app.services.embeddings import EmbeddingProvider, create_embedding_provider
from app.services.extraction import extract_claims
from app.services.fiscal import parse_period_label, period_label
from app.services.ingestion import NO_MANAGEMENT_SPEAKERS, process_document
from app.services.linking import (
    company_as_of,
    group_mentions,
    is_ambiguous,
    load_mentions,
    thread_title,
)
from app.services.llm import LLMProvider, create_llm_provider
from app.services.metrics import MONEY_METRICS, RATE_METRICS, allowed_units
from app.services.periods import end_ordinal, ordinal_label
from app.services.scope import COMPANY
from app.services.storage import PDF_MAGIC, PdfStorage
from app.services.threads import scorecard
from app.services.verification import link_and_verify, load_actuals
from scripts.check_transcript import transcript_warnings

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_PAUSED = 0, 1, 2, 3
PAUSED_MESSAGE = "Paused: Groq daily limit. Run the same command again tomorrow."
TODO_NAME = "actuals_todo.csv"
ACTUALS_NAME = "actuals.csv"

_FILENAME_RE = re.compile(
    r"^(?P<symbol>[A-Za-z0-9&\-]+)_(?P<period>Q[1-4]\s*FY\s*\d{2,4})\.pdf$", re.IGNORECASE
)

Printer = Callable[[str], None]


@dataclass(frozen=True)
class Options:
    symbol: str
    name: str
    sector: str | None
    directory: Path
    actuals: Path | None = None
    allow_fake: bool = False
    reextract: bool = False


@dataclass
class Pipeline:
    """What the steps need; tests build one with fake providers."""

    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    storage: PdfStorage
    embedder: EmbeddingProvider
    llm: LLMProvider


@dataclass(frozen=True)
class Transcript:
    path: Path
    period: str
    fiscal_year: int
    fiscal_quarter: int

    @property
    def ordinal(self) -> int:
        return end_ordinal(self.fiscal_year, self.fiscal_quarter)


@dataclass
class Tokens:
    extraction_in: int = 0
    extraction_out: int = 0
    verify_in: int = 0
    verify_out: int = 0

    @property
    def total(self) -> int:
        return self.extraction_in + self.extraction_out + self.verify_in + self.verify_out


@dataclass(frozen=True)
class TodoRow:
    metric: str
    fiscal_year: int
    fiscal_quarter: int | None
    unit: str
    needed_for: tuple[str, ...] = field(default=())
    scope: str = COMPANY

    @property
    def key(self) -> tuple[str, int, int | None]:
        return self.metric, self.fiscal_year, self.fiscal_quarter

    @property
    def period(self) -> str:
        return period_label(self.fiscal_year, self.fiscal_quarter)


# ------------------------------------------------------------------ files


def find_transcripts(
    directory: Path, symbol: str, max_bytes: int
) -> tuple[list[Transcript], list[str]]:
    """``SYMBOL_Q<n>FY<yy>.pdf`` files directly in ``directory``, oldest quarter first,
    plus a note for every PDF that was skipped."""
    found: dict[int, Transcript] = {}
    skipped: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        match = _FILENAME_RE.match(path.name)
        if match is None:
            skipped.append(f"{path.name}: name is not {symbol}_Q<n>FY<yy>.pdf")
            continue
        if match["symbol"].upper() != symbol:
            skipped.append(f"{path.name}: symbol is not {symbol}")
            continue
        try:
            fiscal_year, fiscal_quarter = parse_period_label(match["period"])
        except ValueError as exc:
            skipped.append(f"{path.name}: {exc}")
            continue
        assert fiscal_quarter is not None  # the pattern requires a quarter
        size = path.stat().st_size
        if size > max_bytes:
            skipped.append(f"{path.name}: {size / 1024 / 1024:.1f} MB is over the upload limit")
            continue
        with path.open("rb") as handle:
            if PDF_MAGIC not in handle.read(1024):
                skipped.append(f"{path.name}: not a PDF (no %PDF- header)")
                continue
        transcript = Transcript(
            path, period_label(fiscal_year, fiscal_quarter), fiscal_year, fiscal_quarter
        )
        if transcript.ordinal in found:
            skipped.append(f"{path.name}: a second file for {transcript.period}")
            continue
        found[transcript.ordinal] = transcript
    return [found[k] for k in sorted(found)], skipped


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------- company/docs


async def ensure_company(session: AsyncSession, opts: Options, say: Printer) -> Company:
    company = await session.scalar(select(Company).where(Company.nse_symbol == opts.symbol))
    if company is None:
        company = Company(name=opts.name, nse_symbol=opts.symbol, sector=opts.sector)
        session.add(company)
        await session.commit()
        await session.refresh(company)
        say(f"Created company {company.name} ({opts.symbol}).")
        return company
    if opts.sector and not company.sector:
        company.sector = opts.sector
        await session.commit()
    if company.name != opts.name:
        say(f"Company {opts.symbol} exists as {company.name!r}; keeping that name.")
    return company


async def upload_transcript(
    pipe: Pipeline, company: Company, transcript: Transcript, say: Printer
) -> uuid.UUID | None:
    """The document for this file: reused if the same file was uploaded before."""
    sha = _sha256(transcript.path)
    async with pipe.sessionmaker() as session:
        existing = await session.scalar(select(Document).where(Document.file_hash == sha))
        if existing is not None:
            if existing.company_id != company.id:
                say(f"  {transcript.path.name}: already uploaded for another company; skipped.")
                return None
            if existing.period_label != transcript.period:
                say(
                    f"  {transcript.path.name}: already uploaded as {existing.period_label}; "
                    "using that document."
                )
            return existing.id
        same_period = await session.scalar(
            select(Document.id).where(
                Document.company_id == company.id,
                Document.doc_type == DocType.TRANSCRIPT,
                Document.fiscal_year == transcript.fiscal_year,
                Document.fiscal_quarter == transcript.fiscal_quarter,
            )
        )
        if same_period is not None:
            say(
                f"  {transcript.path.name}: a different {transcript.period} transcript is "
                f"already uploaded (document {same_period}); using that one. Delete it "
                "first to replace it."
            )
            return same_period
        with transcript.path.open("rb") as handle:
            upload = UploadFile(file=handle, filename=transcript.path.name)
            try:
                document = await ingest_document(
                    session, pipe.storage, company, upload, DocType.TRANSCRIPT, transcript.period
                )
            except AppError as exc:
                say(f"  {transcript.path.name}: upload failed: {exc.message}")
                return None
        say(f"  {transcript.path.name}: uploaded ({document.page_count} pages).")
        return document.id


async def _document(pipe: Pipeline, document_id: uuid.UUID) -> Document:
    async with pipe.sessionmaker() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        return document


async def _chunks(pipe: Pipeline, document_id: uuid.UUID) -> list[Chunk]:
    async with pipe.sessionmaker() as session:
        return list(
            await session.scalars(
                select(Chunk)
                .options(defer(Chunk.embedding))
                .where(Chunk.document_id == document_id)
                .order_by(Chunk.chunk_index)
            )
        )


async def _has_runs(pipe: Pipeline, document_id: uuid.UUID) -> bool:
    async with pipe.sessionmaker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ExtractionRun)
            .where(ExtractionRun.document_id == document_id)
        )
        return bool(count)


async def _latest_prompt_version(pipe: Pipeline, document_id: uuid.UUID) -> str | None:
    async with pipe.sessionmaker() as session:
        version = await session.scalar(
            select(ExtractionRun.prompt_version)
            .where(ExtractionRun.document_id == document_id)
            .order_by(ExtractionRun.started_at.desc())
            .limit(1)
        )
    return None if version is None else str(version)


async def _progress(pipe: Pipeline, document_id: uuid.UUID) -> tuple[int, int]:
    """(management chunks handled, management chunks) for a document."""
    chunks = await _chunks(pipe, document_id)
    management = [c for c in chunks if c.speaker_role is SpeakerRole.MANAGEMENT]
    return sum(1 for c in management if c.claims_extracted_at is not None), len(management)


# --------------------------------------------------------------------- todo


def _todo_unit(metric: str, unit: str | None) -> str:
    allowed = allowed_units(metric)
    if unit and (allowed is None or unit in allowed) and unit != "bps":
        return unit
    if metric in RATE_METRICS:
        return "percent"
    if metric in MONEY_METRICS:
        return "inr_crore"
    return unit or "count"


async def needed_actuals(session: AsyncSession, company_id: uuid.UUID) -> list[TodoRow]:
    """Actuals needed to verify numeric/directional promises whose target period has
    ended (as of the latest call), grouped the way linking groups them: the latest
    mention of each promise sets the period, as in verification."""
    as_of = await company_as_of(session, company_id)
    if as_of is None:
        return []
    grouping = group_mentions(await load_mentions(session, company_id))
    rows: dict[tuple[str, str, int, int | None], TodoRow] = {}

    def need(metric: str, scope: str, fy: int, fq: int | None, unit: str, why: str) -> None:
        key = (metric, scope, fy, fq)
        row = rows.get(key) or TodoRow(metric, fy, fq, unit, scope=scope)
        if why not in row.needed_for:
            row = TodoRow(metric, fy, fq, row.unit, (*row.needed_for, why), scope)
        rows[key] = row

    for members in grouping.groups.members().values():
        mentions = sorted((grouping.mentions[i] for i in members), key=lambda m: m.sort_key)
        latest = mentions[-1]
        if is_ambiguous(latest) or latest.metric is None or latest.target_fiscal_year is None:
            continue
        if latest.target_end is None or latest.target_end > as_of:
            continue
        title = thread_title(mentions)
        unit = _todo_unit(latest.metric, latest.unit)
        need(
            latest.metric,
            latest.scope,
            latest.target_fiscal_year,
            latest.target_fiscal_quarter,
            unit,
            title,
        )
        if latest.claim_type is ClaimType.DIRECTIONAL:
            base = baseline_period(latest)
            if base is not None:
                need(latest.metric, latest.scope, base[0], base[1], unit, f"baseline for {title}")
    return sorted(
        rows.values(), key=lambda r: (r.metric, r.scope, r.fiscal_year, r.fiscal_quarter or 5)
    )


def write_todo(path: Path, symbol: str, rows: list[TodoRow]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow((*COLUMNS, *OPTIONAL_COLUMNS))
        for row in rows:
            writer.writerow(
                [
                    symbol,
                    row.metric,
                    row.fiscal_year,
                    row.fiscal_quarter or "",
                    "",
                    row.unit,
                    "needed for: " + "; ".join(row.needed_for),
                    "",
                    row.scope,
                ]
            )


def filled_rows(content: bytes) -> tuple[bytes, int]:
    """The CSV without rows whose value is empty, and how many were dropped."""
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames or list(COLUMNS))
    writer.writeheader()
    blank = 0
    for row in reader:
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        if not (row.get("value") or "").strip():
            blank += 1
            continue
        writer.writerow(row)
    return out.getvalue().encode("utf-8"), blank


# --------------------------------------------------------------------- steps


async def process_and_extract(
    pipe: Pipeline,
    company: Company,
    documents: list[tuple[Transcript, uuid.UUID]],
    tokens: Tokens,
    say: Printer,
    *,
    reextract: bool = False,
) -> int:
    """Chunk, embed and extract each document. With ``reextract``, a document whose last
    extraction run used another prompt version is extracted again from scratch (its
    claims are replaced); a re-extraction paused by the quota resumes on the next run."""
    skipped = 0
    current = pipe.settings.extraction_prompt_version
    for _, document_id in documents:
        document = await _document(pipe, document_id)
        label = f"{company.nse_symbol} {document.period_label}"
        last_version = await _latest_prompt_version(pipe, document_id)
        rebuild = reextract and last_version is not None and last_version != current
        if document.status is DocumentStatus.EXTRACTED and not rebuild:
            say(f"{label}: already extracted.")
            continue
        if rebuild:
            say(f"{label}: re-extracting with {current} (last run used {last_version}).")
        # An extracted document being rebuilt is already chunked and embedded.
        ready = document.status is DocumentStatus.EMBEDDED or (
            rebuild and document.status is DocumentStatus.EXTRACTED
        )
        if not ready:
            say(f"{label}: chunking and embedding...")
            # A transcript that failed for want of speakers is re-parsed from scratch, so
            # a parser fix takes effect.
            await process_document(
                pipe.sessionmaker,
                pipe.embedder,
                document_id,
                rechunk=_no_speakers(document),
            )
            document = await _document(pipe, document_id)
            if _no_speakers(document):
                say(f"{label}: warning: {document.error_message} Skipped; nothing extracted.")
                skipped += 1
                continue
            if document.status is not DocumentStatus.EMBEDDED:
                say(
                    f"{label}: processing failed: {document.error_message}\n"
                    "Run the same command again to retry."
                )
                return EXIT_FAILED

        if not await _has_runs(pipe, document_id):
            warnings = transcript_warnings(await _chunks(pipe, document_id))
            say(f"{label}: transcript checks: " + ("OK" if not warnings else ""))
            for warning in warnings:
                say(f"  warning: {warning}")

        say(f"{label}: extracting claims...")
        try:
            run = await extract_claims(
                pipe.sessionmaker, pipe.llm, document_id, settings=pipe.settings, reextract=rebuild
            )
        except AppError as exc:
            say(f"{label}: extraction could not start: {exc.message}")
            return EXIT_FAILED
        tokens.extraction_in += run.tokens_in
        tokens.extraction_out += run.tokens_out
        done, total = await _progress(pipe, document_id)
        say(
            f"{label}: run {run.status.value}: {run.claims_created} claims, "
            f"{done}/{total} management chunks handled, "
            f"{run.tokens_in + run.tokens_out} tokens"
        )
        if run.status is ExtractionRunStatus.PAUSED_QUOTA:
            return EXIT_PAUSED
        if _no_speakers(await _document(pipe, document_id)):
            say(f"{label}: warning: {run.error_message} Skipped; nothing extracted.")
            skipped += 1
            continue
        if run.status is not ExtractionRunStatus.COMPLETED:
            say(f"{label}: {run.error_message}\nRun the same command again to retry.")
            return EXIT_FAILED
    if skipped:
        say(
            f"{skipped} transcript(s) had no management speakers and were not extracted "
            "(see the warnings above)."
        )
        return EXIT_FAILED
    return EXIT_OK


def _no_speakers(document: Document) -> bool:
    return document.status is DocumentStatus.FAILED and (document.error_message or "").startswith(
        NO_MANAGEMENT_SPEAKERS
    )


async def print_status(
    pipe: Pipeline, company: Company, documents: list[tuple[Transcript, uuid.UUID]], say: Printer
) -> None:
    say("\nProgress:")
    for _, document_id in documents:
        document = await _document(pipe, document_id)
        done, total = await _progress(pipe, document_id)
        say(
            f"  {company.nse_symbol} {document.period_label}: {document.status.value}, "
            f"{done}/{total} management chunks handled"
        )


async def verify(
    pipe: Pipeline, company: Company, actuals_path: Path, tokens: Tokens, say: Printer
) -> int:
    content, blank = filled_rows(await asyncio.to_thread(actuals_path.read_bytes))
    async with pipe.sessionmaker() as session:
        row = await session.get(Company, company.id)
        assert row is not None
        report = await import_actuals(session, row, content)
    if report.errors:
        say(f"Actuals in {actuals_path.name} were not imported:")
        for error in report.errors:
            say(f"  row {error.row} {error.field or ''}: {error.message}")
        return EXIT_FAILED
    say(
        f"Imported {actuals_path.name}: {report.created} new, {report.updated} updated"
        + (f", {blank} row(s) with no value skipped" if blank else "")
        + "."
    )

    say("Linking and verifying...")
    run = await link_and_verify(
        pipe.sessionmaker, pipe.llm, pipe.embedder, pipe.settings, company.id
    )
    tokens.verify_in += run.tokens_in
    tokens.verify_out += run.tokens_out
    if run.status is not VerificationRunStatus.COMPLETED:
        say(f"Verification failed: {run.error_message}")
        return EXIT_FAILED

    async with pipe.sessionmaker() as session:
        card = await scorecard(session, company.id)
        index = await load_actuals(session, company.id)
        missing = [
            r
            for r in await needed_actuals(session, company.id)
            if index.in_scope(r.scope).resolve(*r.key) is None
        ]
    latest, original = card.say_do_latest, card.say_do_original

    def ratio(value: float | None) -> str:
        return "-" if value is None else f"{value * 100:.1f}%"

    say(f"\nScorecard {company.name} as of {card.as_of_period or '-'}")
    say(
        f"  Say-Do (latest targets):   {latest.kept}/{latest.total} = {ratio(latest.ratio)}"
        + (" (small sample)" if card.say_do.low_sample else "")
    )
    say(f"  Say-Do (original targets): {original.kept}/{original.total} = {ratio(original.ratio)}")
    counts = ", ".join(f"{k} {v}" for k, v in sorted(card.counts.items()) if v)
    say(f"  {card.threads_total} promises: {counts or 'none verified'}")
    for miss in card.top_misses:
        say(f"  missed: {miss.title} ({miss.gap_pct}%)")
    for dropped in card.dropped:
        say(f"  dropped: {dropped.title}")
    if missing:
        say(f"  {len(missing)} needed actual(s) still missing:")
        for r in missing:
            scope = "" if r.scope == COMPANY else f" [{r.scope}]"
            say(f"    {r.metric}{scope} {r.period} ({r.unit})")
    if card.long_term:
        say(f"  {len(card.long_term)} long-term statement(s) with no target period (not scored).")
    return EXIT_OK


async def run(opts: Options, pipe: Pipeline, say: Printer = print) -> int:
    """The whole command; returns the exit code."""
    settings = pipe.settings
    fake = [
        name
        for name, value in (
            ("EMBEDDING_PROVIDER", settings.embedding_provider),
            ("LLM_PROVIDER", settings.llm_provider),
        )
        if value == "fake"
    ]
    if fake and not opts.allow_fake:
        say(
            f"{' and '.join(fake)} is 'fake'; real companies need the real providers "
            "(set them to gemini/groq in backend/.env). --allow-fake is for tests only."
        )
        return EXIT_USAGE
    if settings.embedding_provider == "gemini" and settings.gemini_api_key is None:
        say("GEMINI_API_KEY is not set in backend/.env.")
        return EXIT_USAGE
    if settings.llm_provider == "groq" and settings.groq_api_key is None:
        say("GROQ_API_KEY is not set in backend/.env.")
        return EXIT_USAGE
    if not opts.directory.is_dir():
        say(f"{opts.directory} is not a directory.")
        return EXIT_USAGE

    transcripts, skipped = find_transcripts(opts.directory, opts.symbol, settings.max_upload_bytes)
    for note in skipped:
        say(f"Skipped {note}")
    if not transcripts:
        say(f"No {opts.symbol}_Q<n>FY<yy>.pdf transcripts in {opts.directory}.")
        return EXIT_USAGE

    async with pipe.sessionmaker() as session:
        company = await ensure_company(session, opts, say)

    say(f"{len(transcripts)} transcript(s): {', '.join(t.period for t in transcripts)}")
    documents: list[tuple[Transcript, uuid.UUID]] = []
    for transcript in transcripts:
        document_id = await upload_transcript(pipe, company, transcript, say)
        if document_id is not None:
            documents.append((transcript, document_id))

    tokens = Tokens()
    code = await process_and_extract(
        pipe, company, documents, tokens, say, reextract=opts.reextract
    )
    if code != EXIT_OK:
        await print_status(pipe, company, documents, say)
        say(f"Groq tokens used by this run: {tokens.total}")
        if code == EXIT_PAUSED:
            say(PAUSED_MESSAGE)
        return code
    if len(documents) < len(transcripts):
        say("Some files could not be uploaded (see above); fix them and run again.")
        return EXIT_FAILED

    async with pipe.sessionmaker() as session:
        # The metric/type corrections linking applies, so the todo asks for the right metrics.
        await reclassify_company_claims(session, company.id)
        await session.commit()
        todo = await needed_actuals(session, company.id)
        as_of = await company_as_of(session, company.id)
    todo_path = opts.directory / TODO_NAME
    write_todo(todo_path, opts.symbol, todo)
    say(
        f"All {len(documents)} transcript(s) extracted. Wrote {todo_path.name}: "
        f"{len(todo)} actual(s) needed for promises due by "
        f"{ordinal_label(as_of) if as_of is not None else '-'}."
    )

    actuals_path = opts.actuals or opts.directory / ACTUALS_NAME
    if opts.actuals is not None and not actuals_path.is_file():
        say(f"{actuals_path} does not exist.")
        say(f"Groq tokens used by this run: {tokens.total}")
        return EXIT_USAGE
    if not actuals_path.is_file():
        say(
            f"Next: copy {todo_path.name} to {ACTUALS_NAME}, fill in the values from the "
            "official results and run the same command again."
        )
        say(f"Groq tokens used by this run: {tokens.total}")
        return EXIT_OK
    code = await verify(pipe, company, actuals_path, tokens, say)
    say(f"Groq tokens used by this run: {tokens.total}")
    return code


# ----------------------------------------------------------------------- cli


def parse_args(argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--symbol", required=True, help="NSE symbol, e.g. INFY")
    parser.add_argument("--name", required=True, help='Company name, e.g. "Infosys Ltd"')
    parser.add_argument("--sector", help='e.g. "IT services"')
    parser.add_argument("--dir", required=True, type=Path, help="Folder with the transcripts")
    parser.add_argument("--actuals", type=Path, help="Actuals CSV (default: <dir>/actuals.csv)")
    parser.add_argument(
        "--reextract",
        action="store_true",
        help="Extract again any transcript last extracted with an older prompt version",
    )
    parser.add_argument(
        "--allow-fake", action="store_true", help="Allow fake providers (tests only)"
    )
    args = parser.parse_args(argv)

    def resolve(path: Path) -> Path:
        return path if path.is_absolute() else (BACKEND_DIR / path)

    return Options(
        symbol=args.symbol.strip().upper(),
        name=args.name.strip(),
        sector=(args.sector or "").strip() or None,
        directory=resolve(args.dir),
        actuals=resolve(args.actuals) if args.actuals else None,
        allow_fake=args.allow_fake,
        reextract=args.reextract,
    )


async def main(opts: Options) -> int:
    # Extraction's per-run summary and every dropped past result are logged at INFO.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("app.services.extraction").setLevel(logging.INFO)
    settings = Settings().model_copy(update={"auto_extract": False})
    engine = create_engine(settings.database_url)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    pipe = Pipeline(
        settings=settings,
        sessionmaker=create_sessionmaker(engine),
        storage=PdfStorage(settings.upload_dir, settings.max_upload_bytes),
        embedder=create_embedding_provider(settings),
        llm=create_llm_provider(settings),
    )
    try:
        return await run(opts, pipe)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
