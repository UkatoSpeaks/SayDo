"""Claim extraction: management chunks -> LLM -> validation -> ``claims`` rows.

    embedded -> extract_claims -> extracted

- Only ``speaker_role = management`` chunks are sent. For a Q&A answer the
  analyst question just before it is included as context only; quotes are
  checked against the management chunk alone, so a claim can never come from
  analyst or moderator text.
- A keyword pre-filter (``EXTRACTION_PREFILTER``) skips chunks with no
  forward-looking signal before any LLM call.
- Resumable: every handled chunk gets ``claims_extracted_at`` and the session
  commits after each chunk, so a run paused by the daily quota (status
  ``paused_quota``) or a crash continues from the next unhandled chunk.
- Near-duplicate claims within a document are merged as they arrive; the
  survivor is the highest-confidence version and ``mention_count`` counts them.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer, selectinload

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError
from app.models import (
    Chunk,
    ChunkSection,
    Claim,
    DocType,
    Document,
    DocumentPage,
    DocumentStatus,
    ExtractionRun,
    ExtractionRunStatus,
    SpeakerRole,
)
from app.services.claim_schema import ExtractionResult
from app.services.claim_validation import (
    DedupeKey,
    ValidatedClaim,
    is_duplicate,
    normalize_text,
    validate_claims,
)
from app.services.embeddings import EmbeddingProvider
from app.services.extraction_prompt import PROMPT_VERSION, build_system_prompt, build_user_prompt
from app.services.ingestion import ProcessingTracker, process_document
from app.services.llm import (
    LLMError,
    LLMOutputError,
    LLMProvider,
    LLMQuotaExhausted,
    LLMUsage,
)
from app.services.tokens import get_encoding

logger = logging.getLogger(__name__)

MAX_ERROR_MESSAGE_CHARS = 1000
# Analyst context is trimmed to its last N tokens (the question is usually at the end).
MAX_CONTEXT_TOKENS = 400
# How far back to look past continuation pieces of the same answer for the question.
MAX_CONTEXT_LOOKBACK = 3
_PAGE_PROBE_WORDS = 8

_FORWARD_LOOKING_RE = re.compile(
    r"\b(?:expect\w*|guidance|guid(?:e|es|ed|ing)|target\w*|aim\w*|plan(?:s|ned|ning)?|will"
    r"|\w+'ll|going\s+forward|outlook|anticipat\w*|by\s+fy|next\s+(?:year|quarter|fiscal)"
    r"|h[12]|commission\w*|capex|ramp(?:s|ed|ing)?[\s-]+up|doubl\w*|sustain\w*|maintain\w*"
    r"|trajector\w*)\b",
    re.IGNORECASE,
)


def has_forward_looking_signal(text: str) -> bool:
    return bool(_FORWARD_LOOKING_RE.search(text.replace("\u2019", "'")))


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_message(exc: BaseException) -> str:
    message = str(exc) if isinstance(exc, LLMError) else f"{type(exc).__name__}: {exc}"
    settings = get_settings()
    for secret in (settings.groq_api_key, settings.gemini_api_key):
        if secret is not None and secret.get_secret_value():
            message = message.replace(secret.get_secret_value(), "[redacted]")
    return message[:MAX_ERROR_MESSAGE_CHARS]


# ------------------------------------------------------------------ context


def _tail_tokens(text: str, max_tokens: int) -> str:
    encoding = get_encoding()
    tokens = encoding.encode(text, disallowed_special=())
    if len(tokens) <= max_tokens:
        return text
    return "... " + encoding.decode(tokens[-max_tokens:]).lstrip()


def analyst_context(chunks: list[Chunk], index: int) -> Chunk | None:
    """The analyst chunk a management Q&A chunk answers, if any.

    Looks at the previous chunk, skipping back over earlier pieces of the same
    management answer (a long answer is split into several chunks).
    """
    chunk = chunks[index]
    if chunk.section is not ChunkSection.QNA:
        return None
    j = index - 1
    steps = 0
    while (
        j >= 0 and chunks[j].speaker_role is SpeakerRole.MANAGEMENT and steps < MAX_CONTEXT_LOOKBACK
    ):
        j -= 1
        steps += 1
    if j >= 0 and chunks[j].speaker_role is SpeakerRole.ANALYST:
        return chunks[j]
    return None


# ------------------------------------------------------------------- dedupe


def _key(item: Claim | ValidatedClaim) -> DedupeKey:
    if isinstance(item, Claim):
        return DedupeKey(
            metric_normalized=item.metric_normalized,
            target_fiscal_year=item.target_fiscal_year,
            target_fiscal_quarter=item.target_fiscal_quarter,
            target_low=item.target_low,
            target_high=item.target_high,
            unit=item.unit,
            direction=item.direction.value,
            quote=item.quote,
        )
    return DedupeKey(
        metric_normalized=item.metric_normalized.value,
        target_fiscal_year=item.target_fiscal_year,
        target_fiscal_quarter=item.target_fiscal_quarter,
        target_low=item.target_low,
        target_high=item.target_high,
        unit=item.unit.value if item.unit else None,
        direction=item.direction.value,
        quote=item.quote,
    )


def _apply(claim: Claim, validated: ValidatedClaim) -> None:
    claim.quote = validated.quote
    claim.metric_raw = validated.metric_raw
    claim.metric_normalized = validated.metric_normalized.value
    claim.claim_type = validated.claim_type
    claim.target_low = validated.target_low
    claim.target_high = validated.target_high
    claim.unit = validated.unit.value if validated.unit else None
    claim.direction = validated.direction
    claim.timeframe_raw = validated.timeframe_raw
    claim.target_fiscal_year = validated.target_fiscal_year
    claim.target_fiscal_quarter = validated.target_fiscal_quarter
    claim.timeframe_is_range = validated.timeframe_is_range
    claim.extraction_confidence = validated.confidence
    claim.validation_notes = "; ".join(validated.notes) or None


# ------------------------------------------------------------------ pages


@dataclass
class _PageFinder:
    """Finds the page a quote is on, for chunks that span a page break."""

    pages: dict[int, str]

    def page_of(self, quote: str, page_start: int, page_end: int) -> int:
        if page_start == page_end:
            return page_start
        probe = " ".join(normalize_text(quote).split()[:_PAGE_PROBE_WORDS])
        for page in range(page_start, page_end + 1):
            if probe and probe in self.pages.get(page, ""):
                return page
        return page_start


# -------------------------------------------------------------------- runs


@dataclass
class _RunState:
    claims: list[Claim]
    # chunk id -> (chunk_index, speaker), to spot repeats caused by chunk overlap.
    chunk_positions: dict[uuid.UUID, tuple[int, str | None]] = field(default_factory=dict)
    failed_chunks: list[int] = field(default_factory=list)
    merged: int = 0
    downgraded: int = 0


async def _load_document(session: AsyncSession, document_id: uuid.UUID) -> Document:
    document = await session.scalar(
        select(Document).options(selectinload(Document.company)).where(Document.id == document_id)
    )
    if document is None:
        raise NotFoundError(f"Document {document_id} not found.", code="document_not_found")
    return document


def check_can_extract(document: Document, *, reextract: bool) -> None:
    """Raise ``ConflictError`` unless the document is ready for (re-)extraction."""
    if document.status is DocumentStatus.EXTRACTED and not reextract:
        raise ConflictError(
            "Claims have already been extracted; pass reextract=true to rebuild them.",
            code="already_extracted",
        )
    if document.status not in {DocumentStatus.EMBEDDED, DocumentStatus.EXTRACTED}:
        raise ConflictError(
            f"Document must be embedded before extraction (status is '{document.status.value}').",
            code="document_not_ready",
        )


async def start_extraction(
    session: AsyncSession, document_id: uuid.UUID, *, model: str, reextract: bool = False
) -> ExtractionRun:
    """Validate the document and create a ``running`` run row (committed).

    A ``running`` row left behind by a crashed process is marked failed first;
    callers make sure no run is active in this process (``ProcessingTracker``).
    """
    document = await _load_document(session, document_id)
    check_can_extract(document, reextract=reextract)

    await session.execute(
        update(ExtractionRun)
        .where(
            ExtractionRun.document_id == document_id,
            ExtractionRun.status == ExtractionRunStatus.RUNNING,
        )
        .values(
            status=ExtractionRunStatus.FAILED,
            error_message="Interrupted: the process running it stopped.",
            finished_at=_now(),
        )
    )
    run = ExtractionRun(document_id=document_id, model=model, prompt_version=PROMPT_VERSION)
    session.add(run)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "An extraction run is already active for this document.",
            code="extraction_in_progress",
        ) from exc
    await session.refresh(run)
    return run


async def _reset_document(session: AsyncSession, document: Document) -> None:
    await session.execute(delete(Claim).where(Claim.document_id == document.id))
    await session.execute(
        update(Chunk).where(Chunk.document_id == document.id).values(claims_extracted_at=None)
    )
    document.status = DocumentStatus.EMBEDDED
    await session.commit()
    logger.info("Document %s: claims deleted for re-extraction", document.id)


async def execute_extraction(
    session: AsyncSession,
    run_id: uuid.UUID,
    provider: LLMProvider,
    settings: Settings,
    *,
    reextract: bool = False,
) -> ExtractionRun:
    """Process every pending management chunk of the run's document.

    Commits after each chunk. Ends with the run ``completed`` (document
    ``extracted``), ``paused_quota`` or ``failed``; errors are recorded on the
    run, not raised.
    """
    run = await session.get(ExtractionRun, run_id)
    if run is None:
        raise NotFoundError(f"Extraction run {run_id} not found.", code="run_not_found")
    document = await _load_document(session, run.document_id)
    try:
        if reextract:
            await _reset_document(session, document)
        await _extract(session, run, document, provider, settings)
    except Exception as exc:
        await session.rollback()
        message = _safe_message(exc)
        logger.error("Extraction run %s failed: %s", run_id, message)
        run = await session.get(ExtractionRun, run_id) or run
        run.status = ExtractionRunStatus.FAILED
        run.error_message = message
        run.finished_at = _now()
        await session.commit()
    return run


async def _extract(
    session: AsyncSession,
    run: ExtractionRun,
    document: Document,
    provider: LLMProvider,
    settings: Settings,
) -> None:
    chunks = list(
        await session.scalars(
            select(Chunk)
            .options(defer(Chunk.embedding))
            .where(Chunk.document_id == document.id)
            .order_by(Chunk.chunk_index)
        )
    )
    pending = [
        i
        for i, c in enumerate(chunks)
        if c.speaker_role is SpeakerRole.MANAGEMENT and c.claims_extracted_at is None
    ]
    already_done = sum(
        1
        for c in chunks
        if c.speaker_role is SpeakerRole.MANAGEMENT and c.claims_extracted_at is not None
    )
    run.chunks_total = len(pending)
    await session.commit()
    if already_done:
        logger.info(
            "Document %s: resuming, %d management chunks already handled", document.id, already_done
        )

    pages = await session.execute(
        select(DocumentPage.page_number, DocumentPage.text).where(
            DocumentPage.document_id == document.id
        )
    )
    finder = _PageFinder({number: normalize_text(text) for number, text in pages})
    state = _RunState(
        claims=list(await session.scalars(select(Claim).where(Claim.document_id == document.id))),
        chunk_positions={c.id: (c.chunk_index, c.speaker) for c in chunks},
    )
    system_prompt = build_system_prompt(
        document.company.name, document.fiscal_year, document.fiscal_quarter
    )

    for index in pending:
        chunk = chunks[index]
        if settings.extraction_prefilter and not has_forward_looking_signal(chunk.text):
            chunk.claims_extracted_at = _now()
            run.chunks_skipped += 1
            await session.commit()
            continue

        context = analyst_context(chunks, index)
        user_prompt = build_user_prompt(
            chunk_text=chunk.text,
            speaker=chunk.speaker,
            section=chunk.section,
            context_text=_tail_tokens(context.text, MAX_CONTEXT_TOKENS) if context else None,
            context_speaker=context.speaker if context else None,
        )
        try:
            result, usage = await provider.complete_json(
                system_prompt, user_prompt, ExtractionResult, settings.extraction_model
            )
        except LLMQuotaExhausted as exc:
            _add_usage(run, exc.usage)
            run.status = ExtractionRunStatus.PAUSED_QUOTA
            run.error_message = _safe_message(exc)
            run.finished_at = _now()
            await session.commit()
            logger.warning(
                "Extraction run %s paused (daily quota): %d/%d chunks handled",
                run.id,
                run.chunks_processed + run.chunks_skipped,
                run.chunks_total,
            )
            return
        except LLMOutputError as exc:
            _add_usage(run, exc.usage)
            state.failed_chunks.append(chunk.chunk_index)
            await session.commit()
            logger.warning("Chunk %d: %s", chunk.chunk_index, _safe_message(exc))
            continue
        except LLMError as exc:
            _add_usage(run, exc.usage)
            raise

        _add_usage(run, usage)
        _store_claims(session, run, document, chunk, result, state, finder)
        chunk.claims_extracted_at = _now()
        run.chunks_processed += 1
        await session.commit()

    _finish(run, document, state)
    await session.commit()


def _add_usage(run: ExtractionRun, usage: LLMUsage) -> None:
    run.tokens_in += usage.tokens_in
    run.tokens_out += usage.tokens_out


def _is_overlap_repeat(
    existing: Claim, validated: ValidatedClaim, chunk: Chunk, state: _RunState
) -> bool:
    """The same sentence seen again only because consecutive pieces of one long turn
    overlap by ~50 tokens; that is one mention, not two."""
    if existing.chunk_id is None or existing.chunk_id not in state.chunk_positions:
        return False
    index, speaker = state.chunk_positions[existing.chunk_id]
    return (
        abs(index - chunk.chunk_index) == 1
        and speaker == chunk.speaker
        and normalize_text(existing.quote) == normalize_text(validated.quote)
    )


def _store_claims(
    session: AsyncSession,
    run: ExtractionRun,
    document: Document,
    chunk: Chunk,
    result: ExtractionResult,
    state: _RunState,
    finder: _PageFinder,
) -> None:
    outcome = validate_claims(
        result.claims, chunk.text, document.fiscal_year, document.fiscal_quarter
    )
    run.claims_rejected += outcome.rejected
    state.downgraded += outcome.numbers_downgraded
    if outcome.rejected:
        logger.info(
            "Chunk %d: rejected %d claim(s) whose quote is not in the chunk",
            chunk.chunk_index,
            outcome.rejected,
        )
    for validated in outcome.accepted:
        page = finder.page_of(validated.quote, chunk.page_start, chunk.page_end)
        key = _key(validated)
        duplicate = next((c for c in state.claims if is_duplicate(_key(c), key)), None)
        if duplicate is not None:
            if not _is_overlap_repeat(duplicate, validated, chunk, state):
                duplicate.mention_count += 1
            state.merged += 1
            if validated.confidence > duplicate.extraction_confidence:
                _apply(duplicate, validated)
                duplicate.chunk_id = chunk.id
                duplicate.speaker = chunk.speaker
                duplicate.page_number = page
                duplicate.extraction_run_id = run.id
                duplicate.model = run.model
                duplicate.prompt_version = run.prompt_version
            continue
        claim = Claim(
            company_id=document.company_id,
            document_id=document.id,
            chunk_id=chunk.id,
            speaker=chunk.speaker,
            page_number=page,
            extraction_run_id=run.id,
            model=run.model,
            prompt_version=run.prompt_version,
            mention_count=1,
        )
        _apply(claim, validated)
        session.add(claim)
        state.claims.append(claim)
        run.claims_created += 1


def _finish(run: ExtractionRun, document: Document, state: _RunState) -> None:
    run.finished_at = _now()
    logger.info(
        "Extraction run %s: %d chunks total, %d skipped by pre-filter, %d processed, "
        "%d claims created, %d merged as duplicates, %d rejected (quote not in chunk), "
        "%d downgraded (numbers not in source), %d+%d tokens",
        run.id,
        run.chunks_total,
        run.chunks_skipped,
        run.chunks_processed,
        run.claims_created,
        state.merged,
        run.claims_rejected,
        state.downgraded,
        run.tokens_in,
        run.tokens_out,
    )
    if state.failed_chunks:
        run.status = ExtractionRunStatus.FAILED
        run.error_message = (
            f"{len(state.failed_chunks)} chunk(s) returned invalid output twice "
            f"(chunk_index {', '.join(map(str, state.failed_chunks))}); "
            "start extraction again to retry only those."
        )
        return
    run.status = ExtractionRunStatus.COMPLETED
    if document.status is DocumentStatus.FAILED:
        document.error_message = None
    document.status = DocumentStatus.EXTRACTED


# ------------------------------------------------------------- entry points


async def run_extraction(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    settings: Settings,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    reextract: bool = False,
    tracker: ProcessingTracker | None = None,
) -> None:
    """Background-task wrapper around ``execute_extraction``. Never raises."""
    try:
        async with sessionmaker() as session:
            await execute_extraction(session, run_id, provider, settings, reextract=reextract)
    except Exception:
        logger.exception("Extraction run %s crashed", run_id)
    finally:
        if tracker is not None:
            tracker.finish(document_id)


async def extract_claims(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    document_id: uuid.UUID,
    *,
    reextract: bool = False,
    settings: Settings | None = None,
) -> ExtractionRun:
    """Start and run an extraction in one go (scripts and tests)."""
    settings = settings or get_settings()
    async with sessionmaker() as session:
        run = await start_extraction(
            session, document_id, model=settings.extraction_model, reextract=reextract
        )
        return await execute_extraction(session, run.id, provider, settings, reextract=reextract)


async def process_then_extract(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: EmbeddingProvider,
    llm: LLMProvider,
    settings: Settings,
    document_id: uuid.UUID,
    *,
    rechunk: bool = False,
    tracker: ProcessingTracker | None = None,
) -> None:
    """Chunk + embed, then extract claims when ``AUTO_EXTRACT`` is on. Never raises."""
    try:
        await process_document(sessionmaker, embedder, document_id, rechunk=rechunk)
        if not settings.auto_extract:
            return
        async with sessionmaker() as session:
            document = await session.get(Document, document_id)
            if (
                document is None
                or document.doc_type is not DocType.TRANSCRIPT
                or document.status is not DocumentStatus.EMBEDDED
            ):
                return
            try:
                run = await start_extraction(session, document_id, model=settings.extraction_model)
            except ConflictError as exc:
                logger.info("Auto-extract skipped for %s: %s", document_id, exc.message)
                return
            await execute_extraction(session, run.id, llm, settings)
    except Exception:
        logger.exception("Processing/extraction of document %s crashed", document_id)
    finally:
        if tracker is not None:
            tracker.finish(document_id)
